# Copyright (c) 2021 OpenKS Authors, Visual Computing Group, Beihang University.
# All rights reserved.

import logging
import argparse
import datetime
import time
import json
from tqdm import tqdm
import random
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.distributed as dist
from torch.utils.data import DataLoader, DistributedSampler

from ..model import VisualConstructionModel

from .mmd_modules.HOI.engine import evaluate_hoi, train_one_epoch
from .mmd_modules.HOI.models import build_model
from .mmd_modules.HOI.datasets import build_dataset, get_coco_api_from_dataset
from .mmd_modules.HOI.util import misc as utils


@VisualConstructionModel.register("HOI", "PyTorch")
class VisualRelationTorch(VisualConstructionModel):
    # TODO distributed learning is not complete.
    def __init__(self, name: str = 'pytorch-default', use_distributed: bool = False, args = {"hoi": True}):
        self.name = name
        self.args = self.parse_args(args)
        utils.init_distributed_mode(self.args)
        if self.args.frozen_weights is not None:
            assert self.args.masks, "Frozen training is not suitable for HOI task."
        print(self.args)

        # set the device we need to use
        self.device = torch.device(self.args.device)
        # fix the random seed
        seed = self.args.seed + utils.get_rank()
        torch.manual_seed(seed)
        np.random.seed(seed)
        random.seed(seed)

        # build model
        self.model, self.criterion, self.postprocessors = build_model(self.args)
        self.model.to(self.device)
        self.model_without_ddp = self.model
        if self.args.distributed:
            self.model = torch.nn.parallel.DistributedDataParallel(self.model, device_ids=[self.args.gpu],
                                                                   find_unused_parameters=True)
            self.model_without_ddp = self.model.module
        self.n_parameters = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        print('number of params:', self.n_parameters)

        # build optimizer
        self.param_dicts = [
            {"params": [p for n, p in self.model_without_ddp.named_parameters() if "backbone" not in n and p.requires_grad]},
            {
                "params": [p for n, p in self.model_without_ddp.named_parameters() if "backbone" in n and p.requires_grad],
                "lr": self.args.lr_backbone,
            },
        ]

        self.optimizer = torch.optim.AdamW(self.param_dicts, lr=self.args.lr, weight_decay=self.args.weight_decay)
        self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, self.args.lr_drop)

        self.output_dir = Path(self.args.output_dir)
        self.recorder = utils.RecorderHOI() if self.args.hoi else None
        utils.load_model_weights(self.args, self.model_without_ddp, self.optimizer, self.lr_scheduler, self.recorder)
    
    def parse_args(self, args):
        parser = argparse.ArgumentParser(description="HOI Relation Extraction Model")
        parser.add_argument('--lr', default=1e-4, type=float)
        parser.add_argument('--lr_backbone', default=1e-5, type=float)
        parser.add_argument('--batch_size', default=1, type=int)
        parser.add_argument('--weight_decay', default=1e-4, type=float)
        parser.add_argument('--epochs', default=60, type=int)
        parser.add_argument('--lr_drop', default=40, type=int)
        parser.add_argument('--clip_max_norm', default=0.1, type=float,
                            help='gradient clipping max norm')

        # Model parameters
        parser.add_argument('--frozen_weights', type=str, default=None,
                            help="Path to the pretrained model. If set, only the mask head will be trained")
        # * Backbone
        parser.add_argument('--backbone', default='resnet50', type=str,
                            help="Name of the convolutional backbone to use")
        parser.add_argument('--dilation', action='store_true',
                            help="If true, we replace stride with dilation in the last convolutional block (DC5)")
        parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                            help="Type of positional embedding to use on top of the image features")

        # * Transformer
        parser.add_argument('--enc_layers', default=4, type=int,
                            help="Number of encoding layers in the transformer")
        parser.add_argument('--dec_layers', default=6, type=int,
                            help="Number of decoding layers in the transformer")
        parser.add_argument('--hoi_enc_layers', default=2, type=int,
                            help="Number of hoi encoding layers in the transformer")
        parser.add_argument('--hoi_dec_layers', default=0, type=int,
                            help="Number of hoi decoding layers in the transformer")
        parser.add_argument('--dim_feedforward', default=2048, type=int,
                            help="Intermediate size of the feedforward layers in the transformer blocks")
        parser.add_argument('--dim_feedforward_hoi', default=2048, type=int,
                            help="Intermediate size of the feedforward layers in the hoi transformer blocks")
        parser.add_argument('--hidden_dim', default=256, type=int,
                            help="Size of the embeddings (dimension of the transformer)")
        parser.add_argument('--dropout', default=0.1, type=float,
                            help="Dropout applied in the transformer")
        parser.add_argument('--nheads', default=8, type=int,
                            help="Number of attention heads inside the transformer's attentions")
        parser.add_argument('--nheads_hoi', default=8, type=int,
                            help="Number of attention heads inside the hoi transformer's attentions")
        parser.add_argument('--num_queries', default=100, type=int,
                            help="Number of query slots")
        parser.add_argument('--pre_norm', action='store_true')

        # hoi encoder/decoder
        parser.add_argument('--load_bottleneck_dec_ca_weights', action='store_true')
        parser.add_argument('--hoi_enc_type', type=str, default='hoi_bottleneck')
        parser.add_argument('--vanilla_dec_type', type=str, default='vanilla_bottleneck')
        parser.add_argument('--hoi_dec_type', type=str, default='vanilla')

        # * Segmentation
        parser.add_argument('--masks', action='store_true',
                            help="Train segmentation head if the flag is provided")

        # HOI
        parser.add_argument('--hoi', action='store_true',
                            help="Train for HOI if the flag is provided")
        parser.add_argument('--num_obj_classes', type=int, default=80,
                            help="Number of object classes")
        parser.add_argument('--num_verb_classes', type=int, default=117,
                            help="Number of verb classes")
        parser.add_argument('--pretrained', type=str, default='hoi_params/detr-r50-pre.pth',
                            help='Pretrained model path')
        parser.add_argument('--subject_category_id', default=0, type=int)
        parser.add_argument('--verb_loss_type', type=str, default='focal',
                            help='Loss type for the verb classification')
        parser.add_argument('--verb_gamma', type=float, default=2)
        parser.add_argument('--verb_alpha', type=float, default=None)

        parser.add_argument('--split_query', action='store_true',
                            help="use splitted queries for different branches")
        parser.add_argument('--interact_query', action='store_true',
                            help="interact between different query branches")

        # Loss
        parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                            help="Disables auxiliary decoding losses (loss at each layer)")
        parser.add_argument('--use_matching', action='store_true',
                            help="Use obj/sub matching 2class loss in first decoder, default not use")

        # * Matcher
        parser.add_argument('--set_cost_class', default=1, type=float,
                            help="Class coefficient in the matching cost")
        parser.add_argument('--set_cost_bbox', default=2.5, type=float,
                            help="L1 box coefficient in the matching cost")
        parser.add_argument('--set_cost_giou', default=1, type=float,
                            help="giou box coefficient in the matching cost")
        parser.add_argument('--set_cost_obj_class', default=1, type=float,
                            help="Object class coefficient in the matching cost")
        parser.add_argument('--set_cost_verb_class', default=1, type=float,
                            help="Verb class coefficient in the matching cost")
        parser.add_argument('--box_matcher', default='split_max', type=str)
        parser.add_argument('--set_cost_matching', default=1, type=float,
                            help="Sub and obj box matching coefficient in the matching cost")

        # * Loss coefficients
        parser.add_argument('--mask_loss_coef', default=1, type=float)
        parser.add_argument('--dice_loss_coef', default=1, type=float)
        parser.add_argument('--bbox_loss_coef', default=2.5, type=float)
        parser.add_argument('--giou_loss_coef', default=1, type=float)
        parser.add_argument('--obj_loss_coef', default=1, type=float)
        parser.add_argument('--verb_loss_coef', default=1, type=float)
        parser.add_argument('--eos_coef', default=0.1, type=float,
                            help="Relative classification weight of the no-object class")

        # dataset parameters
        parser.add_argument('--dataset_file', default='hico')
        parser.add_argument('--coco_path', type=str)
        parser.add_argument('--coco_panoptic_path', type=str)
        parser.add_argument('--remove_difficult', action='store_true')
        parser.add_argument('--hoi_path', type=str, default='data/hico_20160224_det')

        parser.add_argument('--output_dir', default='logs_hoi',
                            help='path where to save, empty for no saving')
        parser.add_argument('--device', default='cuda',
                            help='device to use for training / testing')
        parser.add_argument('--seed', default=42, type=int)
        parser.add_argument('--resume', default='', help='resume from checkpoint')
        parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                            help='start epoch')
        parser.add_argument('--eval', action='store_true')
        parser.add_argument('--num_workers', default=4, type=int)

        # distributed training parameters
        parser.add_argument('--world_size', default=1, type=int,
                            help='number of distributed processes')
        parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')

        # decoupling training parameters
        parser.add_argument('--freeze_mode', default=0, type=int)
        parser.add_argument('--obj_reweight', action='store_true')
        parser.add_argument('--verb_reweight', action='store_true')
        parser.add_argument('--use_static_weights', action='store_true',
                            help='use static weights or dynamic weights, default use dynamic')
        parser.add_argument('--queue_size', default=4704 * 1.0, type=float,
                            help='Maxsize of queue for obj and verb reweighting, default 1 epoch')
        parser.add_argument('--p_obj', default=0.7, type=float,
                            help='Reweighting parameter for obj')
        parser.add_argument('--p_verb', default=0.7, type=float,
                            help='Reweighting parameter for verb')

        # hoi eval parameters
        parser.add_argument('--use_nms_filter', action='store_true', help='Use pair nms filter, default not use')
        parser.add_argument('--thres_nms', default=0.7, type=float)
        parser.add_argument('--nms_alpha', default=1.0, type=float)
        parser.add_argument('--nms_beta', default=0.5, type=float)
        parser.add_argument('--json_file', default='results.json', type=str)
        opt = parser.parse_args(args)
        return opt
    
    def evaluate(self):
        dataset_val = build_dataset(image_set='val', args=self.args)
        if self.args.distributed:
            sampler_val = DistributedSampler(dataset_val, shuffle=False)
        else:
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)
        data_loader_val = DataLoader(dataset_val, self.args.batch_size, sampler=sampler_val,
                                     drop_last=False, collate_fn=utils.collate_fn, num_workers=self.args.num_workers)
        test_stats = evaluate_hoi(self.args.dataset_file, self.model, self.postprocessors, 
                                  data_loader_val, self.args.subject_category_id, self.device)
    
    def train(self):
        dataset_train = build_dataset(image_set='train', args=self.args)
        dataset_val = build_dataset(image_set='val', args=self.args)
        if self.args.distributed:
            sampler_train = DistributedSampler(dataset_train)
            sampler_val = DistributedSampler(dataset_val, shuffle=False)
        else:
            sampler_train = torch.utils.data.RandomSampler(dataset_train)
            sampler_val = torch.utils.data.SequentialSampler(dataset_val)
        batch_sampler_train = torch.utils.data.BatchSampler(sampler_train, self.args.batch_size, drop_last=True)

        data_loader_train = DataLoader(dataset_train, batch_sampler=batch_sampler_train,
                                       collate_fn=utils.collate_fn, num_workers=self.args.num_workers)
        data_loader_val = DataLoader(dataset_val, self.args.batch_size, sampler=sampler_val,
                                     drop_last=False, collate_fn=utils.collate_fn, num_workers=self.args.num_workers)
        
        print("开始训练")
        start_time = time.time()
        for epoch in range(self.args.start_epoch, self.args.epochs):
            if self.args.distributed:
                sampler_train.set_epoch(epoch)
    
            train_stats = train_one_epoch(self.model, self.criterion, data_loader_train, self.optimizer, self.device, epoch,
                                          self.args.clip_max_norm)
            self.lr_scheduler.step()

            test_stats = evaluate_hoi(self.args.dataset_file, self.model, self.postprocessors, data_loader_val, 
                                      self.args.subject_category_id, self.device)
            coco_evaluator = hoi_evaluator = None
            utils.save_checkpoints(self.args, self.output_dir, self.recorder, epoch, test_stats,
                                   self.model_without_ddp, self.optimizer, self.lr_scheduler,
                                   hoi_evaluator=hoi_evaluator)
            utils.save_logs(self.args, train_stats, test_stats, epoch, self.n_parameters,
                            self.output_dir, coco_evaluator=coco_evaluator)

        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        print('训练花费时间共计 {}'.format(total_time_str))
        if self.recorder is not None:
            self.recorder.print_best_metrics()
    
    def run(self, mode="train"):
        if mode == "train":
            self.train()
        elif mode == "eval":
            self.evaluate()
        elif mode == "single":
            raise ValueError("UnImplemented mode!")
    