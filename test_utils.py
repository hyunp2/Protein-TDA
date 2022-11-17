import logging
import pathlib
from typing import List, Union
import matplotlib.pyplot as plt
import os, sys
import pathlib
import numpy as np
import torch
import torch.distributed as dist
import torch.nn as nn
import importlib
try:
    importlib.import_module("apex.optimizers")
    from apex.optimizers import FusedAdam, FusedLAMB
except Exception as e:
    pass
from torch.nn.modules.loss import _Loss
from torch.nn.parallel import DistributedDataParallel
from torch.optim import Optimizer
from torch.utils.data import DataLoader, DistributedSampler
from tqdm import tqdm
from transformers import get_linear_schedule_with_warmup
from dist_utils import to_cuda, get_local_rank, init_distributed, seed_everything, \
    using_tensor_cores, increase_l2_fetch_granularity, Logger
# from transformers import AdamW
import curtsies.fmtfuncs as cf
import torchmetrics

# https://fairscale.readthedocs.io/en/latest/
from fairscale.nn.checkpoint.checkpoint_activations import checkpoint_wrapper #anytime
from fairscale.experimental.nn.offload import OffloadModel #Single-GPU
from fairscale.optim.adascale import AdaScale #DDP
from fairscale.nn.data_parallel import ShardedDataParallel as ShardedDDP #Sharding
from fairscale.optim.oss import OSS #Sharding
import shutil
from torch.distributed.fsdp import FullyShardedDataParallel, CPUOffload
from torch.distributed.fsdp.wrap import (
					default_auto_wrap_policy,
					enable_wrap,
					wrap,
					)
from data_utils import get_dataloader
from train_utils import load_state, single_val, single_test

#https://github.com/taki0112/denoising-diffusion-gan-Tensorflow/blob/571a99022ccc07a31b6c3672f7b5b30cd46a7eb6/src/utils.py#L156:~:text=def%20merge(,return%20img
def merge(images, size):
    h, w = images.shape[1], images.shape[2]
    img = np.zeros((h * size[0], w * size[1], 3))
    for idx, image in enumerate(images):
        i = idx % size[1]
        j = idx // size[1]
        img[h * j:h * (j + 1), w * i:w * (i + 1), :] = image

    return img

def plot_analysis(filename: str):
    assert os.path.splitext(filename)[1] == ".npz", "File name extension is wrong..."
    data = np.load(filename)
    keys = list(data)
    fig, ax = plt.subplots(2,1)
    ax[0].hist(data["gt"], bins=100)
    bins, edges, patches = ax[1].hist(data["pred"], alpha=0.5, bins=100)
    fig.savefig("gt_pred.png")
    idx = torch.topk(torch.from_numpy(bins).view(1,-1), dim=-1, k=2).indices #bimodal (1, 2)
    print(idx)
#     idx = torch.topk(torch.from_numpy(bins[idx[0]:idx[1]]).view(1,-1), dim=-1, k=2, largest=False).indices #minimum
#     print(idx)
	
def validate_and_test(model: nn.Module,
          get_loss_func: _Loss,
          train_dataloader: DataLoader,
          val_dataloader: DataLoader,
	  test_dataloader: DataLoader,
          logger: Logger,
          args):
    """Includes evaluation and testing as well!"""

    #DDP options
    #Init distributed MUST be called in run() function, which calls this train function!

    device = torch.cuda.current_device()
    model.to(device=device)
    local_rank = get_local_rank()
    world_size = dist.get_world_size() if dist.is_initialized() else 1
    tmetrics = torchmetrics.MeanAbsoluteError()
    
    #DDP Model
    if dist.is_initialized() and not args.shard:
        model = DistributedDataParallel(model, device_ids=[local_rank], output_device=local_rank)
        model._set_static_graph()
        print(f"DDP is enabled {dist.is_initialized()} and this is local rank {local_rank} and sharding is {args.shard}!!")    
        model.train()
        if args.log: logger.start_watching(model) #watch a model!
    elif dist.is_initialized() and args.shard:
        my_auto_wrap_policy = functools.partial(
            default_auto_wrap_policy, min_num_params=100
        )
        torch.cuda.set_device(local_rank)
        model = FSDP(model, fsdp_auto_wrap_policy=my_auto_wrap_policy)

    init_start_event = torch.cuda.Event(enable_timing=True)
    init_end_event = torch.cuda.Event(enable_timing=True)

    path_and_name = os.path.join(args.load_ckpt_path, "{}.pth".format(args.name))
    assert args.resume, "Validation and test must be under resumed keyword..."
    epoch_start, best_loss = load_state(model, None, None, path_and_name, use_artifacts=args.use_artifacts, logger=logger, name=args.name, model_only=True) 
    
    #DDP training: Total stats (But still across multi GPUs)
    init_start_event.record()
    
    model.eval()    	
	
    ds_train, ds_val, ds_test = train_dataloader.dataset, val_dataloader.dataset, test_dataloader.dataset
    dataloader_kwargs = {'pin_memory': args.pin_memory, 'persistent_workers': args.num_workers > 0,
                                        'batch_size': args.batch_size}
    ds = torch.utils.data.ConcatDataset([ds_train, ds_val, ds_test])
    val_dataloader = get_dataloader(ds, shuffle=False, collate_fn=None, **dataloader_kwargs)
    print(cf.yellow("All the data are concatenated into one! It is still named val_dataloader!"))

    ###EVALUATION
    evaluate = single_val
    val_loss, loss_metrics, val_predictions = evaluate(args, model, val_dataloader, get_loss_func, None, None, logger, tmetrics, return_data=True) #change to single_val with DDP
    if dist.is_initialized():
        val_loss = torch.tensor(val_loss, dtype=torch.float, device=device)
        loss_metrics = torch.tensor(loss_metrics, dtype=torch.float, device=device)
        torch.distributed.all_reduce(val_loss, op=torch.distributed.ReduceOp.SUM)
        val_loss = (val_loss / world_size).item()
        torch.distributed.all_reduce(loss_metrics, op=torch.distributed.ReduceOp.SUM) #Sum to loss
        loss_metrics = (loss_metrics / world_size).item()
    if args.log: 
        logger.log_metrics({'ALL_REDUCED_val_loss': val_loss})
        logger.log_metrics({'ALL_REDUCED_val_MAE': loss_metrics}) #zero rank only
    #zero rank only
    #mae_reduced = tmetrics.compute() #Synced and reduced autometically!
    #logger.log_metrics({'ALL_REDUCED_val_mae_loss': mae_reduced.item()}, epoch_idx) #zero rank only
    tmetrics.reset()
    #scheduler_re.step(val_loss) #Not on individual stats but the total stats

    init_end_event.record()

    if local_rank == 0:
        print(f"CUDA event elapsed time: {init_start_event.elapsed_time(init_end_event) / 1000}sec")
	
    print(cf.on_green("Saving returned validation and test_predictions!"))
    gts = torch.cat([batch["temp"] for batch in val_dataloader], dim=0).reshape(-1,) #B
    val_predictions = val_predictions.detach().cpu().numpy().reshape(-1,) #B
    np.savez("PH_all_test.npz", gt=gts, pred=val_predictions)

#     val_gts = torch.cat([batch["temp"] for batch in val_dataloader], dim=0) #B,1
#     test_gts = torch.cat([batch["temp"] for batch in test_dataloader], dim=0) #B,1
#     np.savez("PH_val_test.npz", validation_gt=val_gts.detach().cpu().numpy(), test_gt=test_gts.detach().cpu().numpy(), validation_pred=val_predictions.detach().cpu().numpy(), test_pred=test_predictions.detach().cpu().numpy())
    
    print(cf.on_yellow("Validation and test are OVER..."))
    dist.destroy_process_group()

if __name__ == "__main__":
    plot_analysis("PH_all_test.npz")
