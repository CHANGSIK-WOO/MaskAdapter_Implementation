"""
This file may have been modified by Bytedance Ltd. and/or its affiliates.
All Bytedance's Modifications are Copyright (year) Bytedance Ltd. and/or its affiliates. 

Reference: https://github.com/facebookresearch/Mask2Former/blob/main/train_net.py

FCCLIP Training Script.

This script is a simplified version of the training script in detectron2/tools.
"""
try:
    # ignore ShapelyDeprecationWarning from fvcore
    from shapely.errors import ShapelyDeprecationWarning
    import warnings
    warnings.filterwarnings('ignore', category=ShapelyDeprecationWarning)
except:
    pass

import copy
import itertools
import logging
import os

from collections import OrderedDict
from typing import Any, Dict, List, Set

import torch

import detectron2.utils.comm as comm
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.config import get_cfg
from detectron2.data import MetadataCatalog, build_detection_train_loader
from detectron2.engine import (
    DefaultTrainer,
    default_argument_parser,
    default_setup,
    launch,
)
from detectron2.evaluation import (
    CityscapesInstanceEvaluator,
    CityscapesSemSegEvaluator,
    COCOEvaluator,
    COCOPanopticEvaluator,
    DatasetEvaluators,
    LVISEvaluator,
    SemSegEvaluator,
    verify_results, 
    # verify_results(cfg, result) : verifcation function which checks whether model is satisfied with benchmark standard or not

)

from mask_adapter.evaluation import SeenUnseenSemSegEvaluator #import SeenUnseenSemSegEvaluator from mask_adapter.evaluation.sem_seg_evaluation.py

from detectron2.projects.deeplab import add_deeplab_config, build_lr_scheduler
from detectron2.solver.build import maybe_add_gradient_clipping
from detectron2.utils.logger import setup_logger

from mask_adapter import (
    COCOInstanceNewBaselineDatasetMapper,
    COCOPanopticNewBaselineDatasetMapper,
    InstanceSegEvaluator,
    MaskFormerInstanceDatasetMapper,
    MaskFormerPanopticDatasetMapper,
    MaskFormerSemanticDatasetMapper,
    SemanticSegmentorWithTTA,
    add_maskformer2_config,
    add_fcclip_config,
    add_mask_adapter_config
)


class Trainer(DefaultTrainer):
    """
    Extension of the Trainer class adapted to FCCLIP.
    (DefaultTrainer + Fcclip + MaskAdapter)
    """

    @classmethod
    def build_evaluator(cls, cfg, dataset_name, output_folder=None):
        """
        Create evaluator(s) for a given dataset.
        This uses the special metadata "evaluator_type" associated with each
        builtin dataset. For your own dataset, you can simply create an
        evaluator manually in your script and do not have to worry about the
        hacky if-else logic here.
        """
        if output_folder is None:
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference") #output directory
        evaluator_list = [] 
        evaluator_type = MetadataCatalog.get(dataset_name).evaluator_type #"ade20k_panoptic_seg", "coco", "coco_panoptic_seg", "sem_seg", "cityscapes_panoptic_seg", "mapillary_vistas_panoptic_seg", "lvis", "cityscapes_instance", "cityscapes_sem_seg"

        # semantic segmentation
        if evaluator_type in ["sem_seg", "ade20k_panoptic_seg"]:
            evaluator_list.append(
                SeenUnseenSemSegEvaluator(
                    dataset_name,
                    distributed=True,
                    output_dir=output_folder,
                )
            )
        # instance segmentation
        if evaluator_type == "coco":
            evaluator_list.append(COCOEvaluator(dataset_name, output_dir=output_folder))

        # panoptic segmentation
        if evaluator_type in [
            "coco_panoptic_seg",
            "ade20k_panoptic_seg",
            "cityscapes_panoptic_seg",
            "mapillary_vistas_panoptic_seg",
        ]:
            if cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON:
                evaluator_list.append(COCOPanopticEvaluator(dataset_name, output_folder))
        # COCO
        if evaluator_type == "coco_panoptic_seg" and cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON:
            evaluator_list.append(COCOEvaluator(dataset_name, output_dir=output_folder))
        if evaluator_type == "coco_panoptic_seg" and cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON:
            evaluator_list.append(SemSegEvaluator(dataset_name, distributed=True, output_dir=output_folder))
        # Mapillary Vistas
        if evaluator_type == "mapillary_vistas_panoptic_seg" and cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON:
            evaluator_list.append(InstanceSegEvaluator(dataset_name, output_dir=output_folder))
        if evaluator_type == "mapillary_vistas_panoptic_seg" and cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON:
            evaluator_list.append(SemSegEvaluator(dataset_name, distributed=True, output_dir=output_folder))
        # Cityscapes
        if evaluator_type == "cityscapes_instance":
            assert (
                torch.cuda.device_count() > comm.get_rank()
            ), "CityscapesEvaluator currently do not work with multiple machines."
            return CityscapesInstanceEvaluator(dataset_name)
        if evaluator_type == "cityscapes_sem_seg":
            assert (
                torch.cuda.device_count() > comm.get_rank()
            ), "CityscapesEvaluator currently do not work with multiple machines."
            return CityscapesSemSegEvaluator(dataset_name)
        if evaluator_type == "cityscapes_panoptic_seg":
            if cfg.MODEL.MASK_FORMER.TEST.SEMANTIC_ON:
                assert (
                    torch.cuda.device_count() > comm.get_rank()
                ), "CityscapesEvaluator currently do not work with multiple machines."
                evaluator_list.append(CityscapesSemSegEvaluator(dataset_name))
            if cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON:
                assert (
                    torch.cuda.device_count() > comm.get_rank()
                ), "CityscapesEvaluator currently do not work with multiple machines."
                evaluator_list.append(CityscapesInstanceEvaluator(dataset_name))
        # ADE20K
        if evaluator_type == "ade20k_panoptic_seg" and cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON:
            evaluator_list.append(InstanceSegEvaluator(dataset_name, output_dir=output_folder))
        # LVIS
        if evaluator_type == "lvis":
            return LVISEvaluator(dataset_name, output_dir=output_folder)
        if len(evaluator_list) == 0:
            raise NotImplementedError(
                "no Evaluator for the dataset {} with the type {}".format(
                    dataset_name, evaluator_type
                )
            )
        elif len(evaluator_list) == 1:
            return evaluator_list[0]
        return DatasetEvaluators(evaluator_list)

    @classmethod
    def build_train_loader(cls, cfg): #image + label --> mapper --> tensor --> dataloader --> minibatch
        # Semantic segmentation dataset mapper
        if cfg.DATALOADER.SAMPLER_TRAIN == "MultiDatasetSampler":
            mapper = COCOCombineNewBaselineDatasetMapper(cfg, True) 
            data_loader = build_custom_train_loader(cfg, mapper=mapper)   
            return data_loader
        else:
            if cfg.INPUT.DATASET_MAPPER_NAME == "mask_former_semantic":
                mapper = MaskFormerSemanticDatasetMapper(cfg, True) 
                return build_detection_train_loader(cfg, mapper=mapper)
            # Panoptic segmentation dataset mapper
            elif cfg.INPUT.DATASET_MAPPER_NAME == "mask_former_panoptic":
                mapper = MaskFormerPanopticDatasetMapper(cfg, True)
                return build_detection_train_loader(cfg, mapper=mapper)
            # Instance segmentation dataset mapper
            elif cfg.INPUT.DATASET_MAPPER_NAME == "mask_former_instance":
                mapper = MaskFormerInstanceDatasetMapper(cfg, True)
                return build_detection_train_loader(cfg, mapper=mapper)
            # coco instance segmentation lsj new baseline
            elif cfg.INPUT.DATASET_MAPPER_NAME == "coco_instance_lsj":
                mapper = COCOInstanceNewBaselineDatasetMapper(cfg, True)
                return build_detection_train_loader(cfg, mapper=mapper)
            # coco panoptic segmentation lsj new baseline
            elif cfg.INPUT.DATASET_MAPPER_NAME == "coco_panoptic_lsj":
                mapper = COCOPanopticNewBaselineDatasetMapper(cfg, True)
                return build_detection_train_loader(cfg, mapper=mapper)
            elif cfg.INPUT.DATASET_MAPPER_NAME == "coco_combine_lsj":
                mapper = COCOCombineNewBaselineDatasetMapper(cfg, True)
                return build_detection_train_loader(cfg, mapper=mapper)
            # elif cfg.INPUT.DATASET_MAPPER_NAME == "grand_panoptic_lsj":
            #     mapper = GrandNewBaselineDatasetMapper(cfg, True)
            #     return build_detection_train_loader(cfg, mapper=mapper)
            else:
                mapper = None
                return build_detection_train_loader(cfg, mapper=mapper)

    @classmethod
    def build_lr_scheduler(cls, cfg, optimizer):
        """
        It now calls :func:`detectron2.solver.build_lr_scheduler`.
        Overwrite it if you'd like a different scheduler.
        """
        return build_lr_scheduler(cfg, optimizer)

    @classmethod
    def build_optimizer(cls, cfg, model):
        weight_decay_norm = cfg.SOLVER.WEIGHT_DECAY_NORM
        weight_decay_embed = cfg.SOLVER.WEIGHT_DECAY_EMBED

        defaults = {} 
        defaults["lr"] = cfg.SOLVER.BASE_LR #   cfg.SOLVER.BASE_LR = 0.0001
        defaults["weight_decay"] = cfg.SOLVER.WEIGHT_DECAY #   cfg.SOLVER.WEIGHT_DECAY = 0.01

        norm_module_types = (
            torch.nn.BatchNorm1d,
            torch.nn.BatchNorm2d,
            torch.nn.BatchNorm3d,
            torch.nn.SyncBatchNorm,
            # NaiveSyncBatchNorm inherits from BatchNorm2d
            torch.nn.GroupNorm,
            torch.nn.InstanceNorm1d,
            torch.nn.InstanceNorm2d,
            torch.nn.InstanceNorm3d,
            torch.nn.LayerNorm,
            torch.nn.LocalResponseNorm,
        )

        params: List[Dict[str, Any]] = []
        memo: Set[torch.nn.parameter.Parameter] = set()
        for module_name, module in model.named_modules():
            #example of module_name : "backbone", "backbone.body", "backbone.body.layer1", "backbone.body.layer1.conv1", "mask_head", "mask_head.mask_decoder"
            #example of module : nn.Module, nn.Linear, nn.Conv2d, nn.BatchNorm2d, nn.LayerNorm, nn.Embedding, etc.
            for module_param_name, value in module.named_parameters(recurse=False):
                #example of module_param_name : "weight", "bias", "relative_position_bias_table", "absolute_pos_embed", "text_projection"
                #example of value : torch.Size([256, 256]), torch.Size([256]), torch.Size([49, 256]), torch.Size([1, 256, 14, 14]), torch.Size([512, 512])
                if not value.requires_grad:
                    continue
                # Avoid duplicating parameters
                if value in memo:
                    continue
                memo.add(value)

                hyperparams = copy.copy(defaults)
                if "backbone" in module_name:
                    hyperparams["lr"] = hyperparams["lr"] * cfg.SOLVER.BACKBONE_MULTIPLIER 
                    # normally, cfg.SOLVER.BACKBONE_MULTIPLIER = 0.1, so backbone learning rate is 10 times smaller than head learning rate.
                    # the reason is that backbone is pretrained, so we don't need to train it with high learning rate.
                if (
                    "relative_position_bias_table" in module_param_name
                    or "absolute_pos_embed" in module_param_name
                ):
                    print(module_param_name)
                    hyperparams["weight_decay"] = 0.0 
                    #position embedding parameters should not be regularized with weight decay.
                if isinstance(module, norm_module_types):
                    hyperparams["weight_decay"] = weight_decay_norm
                    # norm parameters should not be regularized with weight decay.
                if isinstance(module, torch.nn.Embedding):
                    hyperparams["weight_decay"] = weight_decay_embed
                    # embedding parameters should not be regularized with weight decay.
                params.append({"params": [value], **hyperparams})

        def maybe_add_full_model_gradient_clipping(optim):
            # detectron2 doesn't have full model gradient clipping now
            clip_norm_val = cfg.SOLVER.CLIP_GRADIENTS.CLIP_VALUE
            enable = (
                cfg.SOLVER.CLIP_GRADIENTS.ENABLED
                and cfg.SOLVER.CLIP_GRADIENTS.CLIP_TYPE == "full_model"
                and clip_norm_val > 0.0
            )

            class FullModelGradientClippingOptimizer(optim):
                def step(self, closure=None):
                    all_params = itertools.chain(*[x["params"] for x in self.param_groups])
                    torch.nn.utils.clip_grad_norm_(all_params, clip_norm_val)
                    super().step(closure=closure)

            return FullModelGradientClippingOptimizer if enable else optim

        optimizer_type = cfg.SOLVER.OPTIMIZER
        if optimizer_type == "SGD":
            optimizer = maybe_add_full_model_gradient_clipping(torch.optim.SGD)(
                params, cfg.SOLVER.BASE_LR, momentum=cfg.SOLVER.MOMENTUM
            )
        elif optimizer_type == "ADAMW":
            optimizer = maybe_add_full_model_gradient_clipping(torch.optim.AdamW)(
                params, cfg.SOLVER.BASE_LR
            )
        else:
            raise NotImplementedError(f"no optimizer type {optimizer_type}")
        if not cfg.SOLVER.CLIP_GRADIENTS.CLIP_TYPE == "full_model":
            optimizer = maybe_add_gradient_clipping(cfg, optimizer)
        return optimizer

    @classmethod
    def test_with_TTA(cls, cfg, model):
        logger = logging.getLogger("detectron2.trainer")
        # In the end of training, run an evaluation with TTA.
        logger.info("Running inference with test-time augmentation ...")
        model = SemanticSegmentorWithTTA(cfg, model)
        evaluators = [
            cls.build_evaluator(
                cfg, name, output_folder=os.path.join(cfg.OUTPUT_DIR, "inference_TTA")
            )
            for name in cfg.DATASETS.TEST
        ]
        res = cls.test(cfg, model, evaluators)
        res = OrderedDict({k + "_TTA": v for k, v in res.items()}) #OrderedDict : maintain the order of keys. if use Dict, the order of keys may be changed.
        return res


def setup(args):
    """
    Create configs and perform basic setups.
    """
    cfg = get_cfg() #make instance of CfgNode Normal to Normal Setting.
    #example (.yaml)
    #MODEL:
        #META_ARCHTECTURE : "RCNN"
        #WEIGHTS: ""
        #MASK_ON: FALSE
    
    #INPUT
        #FORMAT: "BGR"...



    # for poly lr schedule
    add_deeplab_config(cfg)
    add_maskformer2_config(cfg)
    add_fcclip_config(cfg)
    add_mask_adapter_config(cfg)
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts) #cfg.merge_from_list(args.opts) : merge from list of options. ex) --opts MODEL.WEIGHTS "path/to/weights.pth"
    cfg.freeze()
    default_setup(cfg, args)
    # Setup logger for "fcclip" module
    setup_logger(output=cfg.OUTPUT_DIR, distributed_rank=comm.get_rank(), name="mask_adapter")
    return cfg


def main(args):
    cfg = setup(args) 
    # python train_net_maskadapter.py --config-file configs/fcclip/fcclip_r50_8xb2-100k_coco_panoptic.yaml --eval-only
    # args.config_file = "configs/fcclip/fcclip_r50_8xb2-100k_coco_panoptic.yaml"
    # setup(args) -> get_cfg() -> cfg.merge_from_file(args.config_file) -> cfg.merge_from_list(args.opts) -> cfg.freeze()

    if args.eval_only: #if args.eval_only == True, that is evaluation mode using checkpoints
        model = Trainer.build_model(cfg) 
        #call build_model(cfg) -> meta_arch_catalog[cfg.MODEL.META_ARCHITECTURE] (ex) execute "GeneralizedRCNN(cfg)" -> nn.Module Return

        total_params = sum(p.numel() for p in model.parameters()) #parameter num return
        trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad) #requires_grad = trainable
        frozen_params = sum(p.numel() for p in model.parameters() if not p.requires_grad) # not requires_grad = not trainable
        frozen_params_exclude_text = 0 # frozen parameter num excluding text_related params.
        for n, p in model.named_parameters(): 
        #n : parameter name, p : parameter tensor
        #linear1.weight torch.size([5, 10])
        #linear1.bias torch.size([5])
        #bn1.weight torch.size([5])
        #bn1.bias torch.size([5])

            if p.requires_grad: #if tranable,
                continue
            # ignore text tower (if ralated to text)
            if 'clip_model.token_embedding' in n or 'clip_model.positional_embedding' in n or 'clip_model.transformer' in n or 'clip_model.ln_final' in n or 'clip_model.text_projection' in n:
            # clip_model.token_embedding : nn.Embedding, clip_model.ln_final = Text Encoder's final LayerNorm
                continue
            frozen_params_exclude_text += p.numel() #only visual frozen
        print(f"total_params: {total_params}, trainable_params: {trainable_params}, frozen_params: {frozen_params}, frozen_params_exclude_text: {frozen_params_exclude_text}")

        DetectionCheckpointer(model, save_dir=cfg.OUTPUT_DIR).resume_or_load(
            cfg.MODEL.WEIGHTS, resume=args.resume
        )
        #cfg.MODEL.WEIGHTS : Path of model weight
        #if resume == True, resume with optimizer, or just load only weight.
        #sequence = model.state_dict() -> torch.load(path) -> model.load_state_dict(checkpoint["model"])

        res = Trainer.test(cfg, model) 
        #just test to validation dataset (res = mIoU, mask AP, and etc.)
        # {'bbox/AP' : 42.1, 'segm/AP' : 37.5}
        if cfg.TEST.AUG.ENABLED:
            res.update(Trainer.test_with_TTA(cfg, model)) # return result with TTA Test res added.
        # result = {'bbox/AP' : 42.1, 'segm/AP' : 37.5, 'bbox/AP_tta' : 43.2, 'segm/AP_tta' : 38.4}
        if comm.is_main_process():
            verify_results(cfg, res) #just verify and make result in only main process. (rank 0)
        return res

    trainer = Trainer(cfg)
    trainer.resume_or_load(resume=args.resume)
    return trainer.train()


if __name__ == "__main__":
    args = default_argument_parser().parse_args()
    print("Command Line Args:", args)
    launch(
        main, #define main function
        args.num_gpus,
        num_machines=args.num_machines, #GPU Number for distributed Learning
        machine_rank=args.machine_rank, #check machine rank in multiple GPU
        dist_url=args.dist_url, 
        args=(args,), #args tuple. inputted in main arguments
    )
