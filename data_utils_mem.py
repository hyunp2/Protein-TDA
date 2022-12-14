from __future__ import print_function, division
import abc, sys
import collections
import torch_geometric
from torch_geometric.data import Data, Dataset
import pathlib
# roots = pathlib.Path(__file__).parent.parent
# sys.path.append(roots) #append top directory
import glob
import persim
import ripser
import MDAnalysis as mda
import argparse
from typing import *
import functools
import itertools 
import functools
import numpy as np
import pandas as pd
import time
import ray
import os
import pickle
import collections
import warnings
import curtsies.fmtfuncs as cf
import tqdm
import pymatgen as pg
from pymatgen.core import Structure
import dataclasses
import torch
from torch.utils.data.dataloader import default_collate
from torch.utils.data.sampler import SubsetRandomSampler
from torch_geometric.loader import DataLoader #Can this handle DDP? yeah!
import torch.distributed as dist 
from dist_utils import to_cuda, get_local_rank, init_distributed, seed_everything, \
    using_tensor_cores, increase_l2_fetch_granularity
from torch.utils.data import DistributedSampler
from typing import *
from topologylayer.nn import RipsLayer, AlphaLayer
import gc
from MDAnalysis.analysis.base import AnalysisFromFunction
from MDAnalysis.analysis.align import AlignTraj
from MDAnalysis import transformations
from math_utils import wasserstein
from main import get_args
import re

__all__ = ["PH_Featurizer_Dataset", "PH_Featurizer_DataLoader"]

warnings.simplefilter("ignore")
warnings.filterwarnings("ignore")

import torch
import numpy as np

def remove_filler(dgm, val=np.inf):
    """
    remove filler rows from diagram
    """
    inds = (dgm[:,0] != val)
    return dgm[inds,:]

def remove_zero_bars(dgm):
    """
    remove zero bars from diagram
    """
    inds = dgm[:,0] != dgm[:,1]
    return dgm[inds,:]

def remove_infinite_bars(dgm, issub):
    """
    remove infinite bars from diagram
    """
    if issub:
        inds = dgm[:, 1] != np.inf
        return dgm[inds,:]
    else:
        inds = dgm[:, 1] != -np.inf
        return dgm[inds,:]

def order_dgm(dgm):
    dgm = remove_zero_bars(dgm)
    dgm = remove_infinite_bars(dgm, True)
    order_data = np.abs(dgm[:,1] - dgm[:,0]) # abs(death - birth)
    args = np.argsort(order_data, axis=0) #Largest to smallest length
    dgm = dgm[args]
    return dgm
    
def _get_split_sizes(train_frac: float, full_dataset: Dataset) -> Tuple[int, int, int]:
    """DONE: Need to change split schemes!"""
    len_full = len(full_dataset)
    len_train = int(len_full * train_frac)
    len_test = int(0.1 * len_full)
    len_val = len_full - len_train - len_test
    return len_train, len_val, len_test  
  
def get_dataloader(dataset: Dataset, shuffle: bool, collate_fn: callable=None, **kwargs):
    sampler = DistributedSampler(dataset, shuffle=shuffle) if dist.is_initialized() else None
    loader = DataLoader(dataset, shuffle=(shuffle and sampler is None), sampler=sampler, collate_fn=collate_fn, **kwargs)
    return loader

def get_coordinates(filenames: List[str]):
    """Logic to return coordinates of files"""
    structure: callable = lambda cif: Structure.from_file(cif).cart_coords
    coords_list: List[np.ndarray] = list(map(lambda inp: structure(inp), filenames ))
    return coords_list

@ray.remote
def get_coordinates_mp(filename: str):
    """Logic to return coordinates of files"""
    structure: callable = lambda cif: Structure.from_file(cif).cart_coords
    coords = structure(filename)
    return coords

def persistent_diagram(graph_input_list: List[np.ndarray], maxdim: int):
    assert isinstance(graph_input_list, list), f"graph_input_list must be a type list..."
    Rs_total = list(map(lambda info: ripser.ripser(info, maxdim=maxdim)["dgms"], graph_input_list ))
    return Rs_total

@ray.remote
def persistent_diagram_mp(graph_input: np.ndarray, maxdim: int, tensor: bool=False):
    assert isinstance(graph_input, (torch.Tensor, np.ndarray)), f"graph_input must be a type array..."
    #Definition of information has changed from List[np.ndarray] to np.ndarray
    #Multiprocessing changes return value from "List of R" to "one R"
    graph_input = graph_input.detach().cpu().numpy()
    if not tensor:
        R_total = ripser.ripser(graph_input, maxdim=maxdim)["dgms"]
    else:
#         graph_input = torch.from_numpy(graph_input).to("cuda").type(torch.float)
#         layer = RipsLayer(graph_input.size(0), maxdim=maxdim)
        layer = AlphaLayer(maxdim=maxdim)
        layer.cuda()
        R_total = layer(graph_input)
    return R_total

# @ray.remote
def persistent_diagram_tensor(graph_input: torch.Tensor, maxdim: int):
    assert isinstance(graph_input, torch.Tensor), f"graph_input must be a type array..."
    #Definition of information has changed from List[np.ndarray] to np.ndarray
    #Multiprocessing changes return value from "List of R" to "one R"
#     layer = RipsLayer(graph_input.size(0), maxdim=maxdim)
    layer = AlphaLayer(maxdim=maxdim)
    layer #.to(torch.cuda.current_device())
    R_total = layer(graph_input)
    return R_total

def traj_preprocessing(prot_traj, prot_ref, align_selection):
    if (prot_traj.trajectory.ts.dimensions is not None): 
        box_dim = prot_traj.trajectory.ts.dimensions
    else:
        box_dim = np.array([1,1,1,90,90,90])
#         print(box_dim, prot_traj.atoms.positions, prot_ref.atoms.positions, align_selection)
    transform = transformations.boxdimensions.set_dimensions(box_dim)
    prot_traj.trajectory.add_transformations(transform)
    #AlignTraj(prot_traj, prot_ref, select=align_selection, in_memory=True).run()
    return prot_traj
    
# @dataclasses.dataclass
class PH_Featurizer_Dataset(Dataset):
    def __init__(self, args: argparse.ArgumentParser):
        super().__init__()
        self.step = 0
        [setattr(self, key, val) for key, val in args.__dict__.items()]
#         self.files_to_pg = list(map(lambda inp: os.path.join(self.data_dir, inp), os.listdir(self.data_dir)))
#         self.files_to_pg = list(filter(lambda inp: os.path.splitext(inp)[-1] == ".cif", self.files_to_pg ))
#         self.reference, self.prot_traj = self.load_traj(data_dir=self.data_dir, pdb=self.pdb, psf=self.psf, trajs=self.trajs, selection=self.atom_selection)
#         self.coords_ref, self.coords_traj = self.get_coordinates_for_md(self.reference), self.get_coordinates_for_md(self.prot_traj)
        _temps = np.repeat(sorted(glob.glob(os.path.join(self.save_dir, "TEMP*dat"))), 4).tolist() #4 is 4 patching scheme by Andres
        _coords = sorted(glob.glob(os.path.join(self.save_dir, "coords*pickle")))
        _phs = sorted(glob.glob(os.path.join(self.save_dir, "PH*pickle")))
#         print(_temps)
        zips = zip(_coords, _phs, _temps) #zip to shortest

        self.graph_input_list, self.Rs_total, self.mem_temp_list, self.Rs_list_tensor = list(zip(*[self.get_values(coord_filename, ph_filename, temp_filename) for coord_filename, ph_filename, temp_filename in zips]))
        #ABOVE: self.graph_input_list is a LIST of <list of coordinates>; number of elements is number of pickle files
#         print(self.graph_input_list)
        if not self.ignore_topologicallayer:
            self.graph_input_list, self.Rs_total, self.mem_temp_list, self.Rs_list_tensor = list(map(lambda one_list: functools.reduce(lambda a, b: a+b, one_list ), (self.graph_input_list, self.Rs_total, self.mem_temp_list, self.Rs_list_tensor) ))
        else:
            self.graph_input_list, self.Rs_total, self.mem_temp_list = list(map(lambda one_list: functools.reduce(lambda a, b: a+b, one_list ), (self.graph_input_list, self.Rs_total, self.mem_temp_list) ))
        #ABOVE: self.graph_input_list is a <list of coordinates>; number of elements is number of pickle files * num_coords per file
        self.min_t, self.max_t = np.min(self.mem_temp_list), np.max(self.mem_temp_list)
        self.mem_temp_list = ((1 - (-1)) * (np.array(self.mem_temp_list) - self.min_t) / (self.max_t - self.min_t) + (-1)).tolist() #scale to (-1,1)
        #ABOVE: https://stackoverflow.com/questions/5294955/how-to-scale-down-a-range-of-numbers-with-a-known-min-and-max-value#:~:text=f(x)%20%3D%20%2D%2D%2D%2D%2D%2D%2D%2D%2D%20%20%20%3D%3D%3D%3E%20%20%20f(min)%20%3D%200%3B%20%20f(max)%20%3D%20%20%2D%2D%2D%2D%2D%2D%2D%2D%2D%20%3D%201
#         del self.coords_ref
#         del self.coords_traj
#         gc.collect()
        
    def get_persistent_diagrams(self, coord_filename, ph_filename, temp_filename):
        self.step += 1
        print(f"Parsing {self.step}-th file...")
        temperature = os.path.splitext(os.path.split(temp_filename)[1])[0] #remove .dat
#         print(temperature)
        temperature = re.split(r"[.|_]", temperature)[1]
#         print(temperature[1])
        ph_temperature = os.path.splitext(os.path.split(ph_filename)[1])[0] #remove .pickle
#         print(ph_temperature)
        ph_temperature = re.split(r"[.|_]", ph_temperature)[1]
        assert temperature == ph_temperature, "temperature must be the same..."
        
        f = open(os.path.join(self.save_dir, coord_filename), "rb")
        graph_input_list = pickle.load(f) #List of structures: each structure has maxdim PHs
        graph_input_list = list(map(lambda inp: torch.tensor(inp), graph_input_list ))[slice(1,None)] #List of (L,3) Arrays; except for the beginning (i.e. ref)
        f.close()
        f = open(os.path.join(self.save_dir, ph_filename), "rb")
        Rs_total = pickle.load(f)[slice(1,None)] #List of structures: each structure has maxdim PHs ; except for the beginning (i.e. ref)
        dcdlen = len(Rs_total) #slice last 200 frames?
        mem_temp_list = pd.read_csv(os.path.join(self.save_dir, temp_filename), delim_whitespace=True).values[-dcdlen:, 1].reshape(-1, ).astype(float).tolist() #get last dcdlen frames of temperatures
        f.close()
        
        maxdims = [self.maxdim] * len(graph_input_list)
        if not self.ignore_topologicallayer: Rs_list_tensor = list(map(alphalayer_computer_coords, graph_input_list, maxdims ))
        if self.preprocessing_only or self.ignore_topologicallayer:
            return graph_input_list, Rs_total, mem_temp_list, None #List of structures: each structure has maxdim PHs
        else:
            return graph_input_list, Rs_total, mem_temp_list, Rs_list_tensor #List of structures: each structure has maxdim PHs

    def get_values(self, coord_filename, ph_filename, temp_filename):
        graph_input_list, Rs_total, mem_temp_list, Rs_list_tensor = self.get_persistent_diagrams(coord_filename, ph_filename, temp_filename)
        return graph_input_list, Rs_total, mem_temp_list, Rs_list_tensor

    def len(self, ):
        return len(self.graph_input_list)

    def get(self, idx):
        if self.preprocessing_only:
            raise NotImplementedError("Get item method is not available with preprocessing_only option!")
#         graph_input = torch.from_numpy(self.graph_input_list[idx]).type(torch.float)
        graph_input = self.graph_input_list[idx].type(torch.float)
        mem_temp = torch.tensor([self.mem_temp_list[idx]]).type(torch.float) #(b, 1)
        if self.ignore_topologicallayer:
            Rs = self.Rs_total[idx]
            Rs_dict = dict()
            for i in range(self.maxdim+1):
                Rs_dict[f"ph{i}"] = torch.from_numpy(Rs[i]).type(torch.float)
        else:
            Rs = list(self.Rs_list_tensor[idx])
            Rs_dict = dict()
    #         Rs_list_tensor = list(persistent_diagram_tensor(graph_input, maxdim=self.maxdim))
            del Rs[0] #Remove H0
            for i in range(1, self.maxdim+1):
                Rs_dict[f"ph{i}"] = order_dgm(Rs[i-1]) #ordered!
            
        return {"Coords": Data(x=graph_input, y=mem_temp), "PH": Data(x=Rs_dict["ph1"], **Rs_dict)}
    
    def load_traj(self, data_dir: str, pdb: str, psf: str, trajs: List[str], selection: str):
        assert (pdb is not None) or (psf is not None), "At least either PDB of PSF should be provided..."
        assert trajs is not None, "DCD(s) must be provided"
        top = pdb if (pdb is not None) else psf
        top = os.path.join(data_dir, top)
        trajs = list(map(lambda inp: os.path.join(data_dir, inp), trajs ))
        universe = mda.Universe(top, *trajs)
        reference = mda.Universe(top)
        print("MDA Universe is created")
    #         print(top, universe,reference)
        #prot_traj = traj_preprocessing(universe, reference, selection)
        prot_traj = universe
        print("Aligned MDA Universe is RETURNED!")

        return reference, prot_traj #universes

    def get_coordinates_for_md(self, mda_universes_or_atomgroups: mda.AtomGroup):
        ags = mda_universes_or_atomgroups #List of AtomGroups 
        assert isinstance(ags, (mda.AtomGroup, mda.Universe)), "mda_universes_or_atomgroups must be AtomGroup or Universe!"

        prot_traj = ags.universe if hasattr(ags, "universe") else ags #back to universe
        coords = AnalysisFromFunction(lambda ag: ag.positions.copy(),
                               prot_traj.atoms.select_atoms(self.atom_selection)).run().results['timeseries'] #B,L,3
        information = torch.from_numpy(coords).unbind(dim=0) #List of (L,3) Tensors
#         information = list(map(lambda inp: inp.detach().cpu().numpy(), information )) #List of (L,3) Arrays

        return information
    
class PH_Featurizer_DataLoader(abc.ABC):
    """ Abstract DataModule. Children must define self.ds_{train | val | test}. """

    def __init__(self, **dataloader_kwargs):
        super().__init__()
        self.opt = opt = dataloader_kwargs.pop("opt")
        self.dataloader_kwargs = {'pin_memory': opt.pin_memory, 'persistent_workers': opt.num_workers > 0,
                                        'batch_size': opt.batch_size}

        if get_local_rank() == 0:
            self.prepare_data()
            print(f"{get_local_rank()}-th core is parsed!")
#             self.prepare_data(opt=self.opt, data=self.data, mode=self.mode) #torch.utils.data.Dataset; useful when DOWNLOADING!

        # Wait until rank zero has prepared the data (download, preprocessing, ...)
        if dist.is_initialized():
            dist.barrier(device_ids=[get_local_rank()]) #WAITNG for 0-th core is done!
                    
        self.full_dataset = full_dataset = PH_Featurizer_Dataset(self.opt)
        self.ds_train, self.ds_val, self.ds_test = torch.utils.data.random_split(full_dataset, _get_split_sizes(self.opt.train_frac, full_dataset),
                                                                generator=torch.Generator().manual_seed(42))
    
    def prepare_data(self, ):
        """ Method called only once per node. Put here any downloading or preprocessing """
        full_dataset = PH_Featurizer_Dataset(self.opt)
#         full_dataset.get_values()
        print(cf.on_blue("Preparation is done!"))
            
    def train_dataloader(self) -> DataLoader:
        return get_dataloader(self.ds_train, shuffle=True, collate_fn=None, **self.dataloader_kwargs)

    def val_dataloader(self) -> DataLoader:
        return get_dataloader(self.ds_val, shuffle=False, collate_fn=None, **self.dataloader_kwargs)

    def test_dataloader(self) -> DataLoader:
        return get_dataloader(self.ds_test, shuffle=False, collate_fn=None, **self.dataloader_kwargs)    

def alphalayer_computer(batches: Data, maxdim: int):
    batches = batches.to(torch.cuda.current_device())
    poses = batches.x
    batch = batches.batch
    phs = []
    pos_list = []
    for b in batch.unique():
        sel = (b == batch)
        pos = poses[sel]
        pos_list.append(pos)
        ph, _ = persistent_diagram_tensor(pos, maxdim=maxdim)
        phs.append(ph)
    return phs #List[List[torch.Tensor]]   

def alphalayer_computer_coords(coords: torch.Tensor, maxdim: int):
#     coords = coords.to(torch.cuda.current_device())
    ph, _ = persistent_diagram_tensor(coords, maxdim=maxdim)
    return ph #List[List[torch.Tensor]]  

if __name__ == "__main__":
    args = get_args()
    ph = PH_Featurizer_Dataset(args)
#     print(ph[5])
#     dataloader = PH_Featurizer_DataLoader(opt=args)
#     print(iter(dataloader.test_dataloader()).next())
#     for i, batches in enumerate(dataloader.train_dataloader()):
# #     batches = iter(dataloader.test_dataloader()).next() #num_nodes, 3
#         phs = alphalayer_computer(batches, ph.maxdim)
#         print(phs)
#         print(f"{i} is done!")
#     maxdims = [ph.maxdim] * batch.unique().size(0)
#     tensor_flags = [ph.tensor] * batch.unique().size(0)
#     futures = [persistent_diagram_tensor.remote(i, maxdim, tensor_flag) for i, maxdim, tensor_flag in zip(pos_list, maxdims, tensor_flags)] 
#     Rs_total = ray.get(futures) #List of structures: each structure has maxdim PHs
    #     print(phs)
    # graph_input_list, Rs_total = ph
    # print(graph_input_list[0], Rs_total[0])

    # python -m data_utils --psf reference_autopsf.psf --pdb reference_autopsf.pdb --trajs adk.dcd --save_dir . --data_dir /Scr/hyunpark/Monster/vaegan_md_gitlab/data --multiprocessing --filename temp2.pickle
