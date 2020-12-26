import os
import pickle
import sys
import time
import math

import torch
import torch.nn as nn
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel

from uer.model_loader import load_model
from uer.model_saver import save_model
from uer.model_builder import build_model
from uer.utils.optimizers import *
from uer.utils import *
from uer.utils.vocab import Vocab
from uer.utils.seed import set_seed

def count_labels_num(path):
    labels_set = set()
    file = open(path, "rb")
    while True:
        try:
            _, tgt, _ = pickle.load(file)
            labels_set.add(tgt)
        except EOFError:
            break
    return len(labels_set)


def train_and_validate(args):
    set_seed(args.seed)

    # Load vocabulary.
    if args.spm_model_path:
        try:
            import sentencepiece as spm
        except ImportError:
            raise ImportError("You need to install SentencePiece to use XLNetTokenizer: https://github.com/google/sentencepiece"
                              "pip install sentencepiece")
        sp_model = spm.SentencePieceProcessor()
        sp_model.Load(args.spm_model_path)
        args.vocab = {sp_model.IdToPiece(i): i for i
                      in range(sp_model.GetPieceSize())}
        if args.target == "mt":
            tgt_sp_model = spm.SentencePieceProcessor()
            tgt_sp_model.Load(args.tgt_spm_model_path)
            args.tgt_vocab = {tgt_sp_model.IdToPiece(i): i for i
                              in range(tgt_sp_model.GetPieceSize())}
    else:
        vocab = Vocab()
        vocab.load(args.vocab_path)
        args.vocab = vocab.w2i
        if args.target == "mt":
            tgt_vocab = Vocab()
            tgt_vocab.load(args.tgt_vocab_path)
            args.tgt_vocab = tgt_vocab.w2i
    if args.target == "cls":
        args.labels_num = count_labels_num(args.dataset_path)
    # Build model.
    model = build_model(args)

    # Load or initialize parameters.
    if args.pretrained_model_path is not None:
        # Initialize with pretrained model.
        model = load_model(model, args.pretrained_model_path) 
    else:
        # Initialize with normal distribution.
        for n, p in list(model.named_parameters()):
            if 'gamma' not in n and 'beta' not in n:
                p.data.normal_(0, 0.02)

    if args.dist_train:
        # Multiprocessing distributed mode.
        mp.spawn(worker, nprocs=args.ranks_num, args=(args.gpu_ranks, args, model), daemon=False)
    elif args.single_gpu:
        # Single GPU mode.
        worker(args.gpu_id, None, args, model)
    else:
        # CPU mode.
        worker(None, None, args, model)


def train_bert(args, gpu_id, rank, loader, model, optimizer, scheduler):
    model.train()
    start_time = time.time()
    total_loss, total_loss_mlm, total_loss_nsp = 0., 0., 0.
    # Calculate MLM accuracy.
    total_correct_mlm, total_denominator = 0., 0. 
    # Calculate NSP accuracy.
    total_correct_nsp, total_instances = 0., 0.
    steps = 1
    total_steps = args.total_steps
    done_tokens = 0
    loader_iter = iter(loader)

    while True:
        if steps == total_steps + 1:
            break
        src, tgt_mlm, tgt_nsp, seg = next(loader_iter)

        if gpu_id is not None:
            src = src.cuda(gpu_id)
            tgt_mlm = tgt_mlm.cuda(gpu_id)
            tgt_nsp = tgt_nsp.cuda(gpu_id)
            seg = seg.cuda(gpu_id)

        # Forward.
        loss_info = model(src, (tgt_mlm, tgt_nsp), seg)
        loss_mlm, loss_nsp, correct_mlm, correct_nsp, denominator = loss_info
        
         # Backward.
        loss = loss_mlm + loss_nsp
        total_loss += loss.item()
        total_loss_mlm += loss_mlm.item()
        total_loss_nsp += loss_nsp.item()
        total_correct_mlm += correct_mlm.item()
        total_correct_nsp += correct_nsp.item()
        total_denominator += denominator.item()
        total_instances += src.size(0)
        done_tokens += src.size(0) * src.size(1)

        loss = loss / args.accumulation_steps

        if args.fp16:
            with args.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if steps % args.accumulation_steps == 0:
            optimizer.step()
            scheduler.step()
            model.zero_grad()
        
        if steps % args.report_steps == 0  and \
            (not args.dist_train or (args.dist_train and rank == 0)):

            loss = total_loss / args.report_steps
            loss_mlm = total_loss_mlm / args.report_steps
            loss_nsp = total_loss_nsp / args.report_steps

            elapsed = time.time() - start_time

            if args.dist_train:
                done_tokens *= args.world_size

            print("| {:8d}/{:8d} steps"
                  "| {:8.2f} tokens/s"
                  "| loss {:7.2f}"
                  "| loss_mlm: {:3.3f}"
                  "| loss_nsp: {:3.3f}"
                  "| acc_mlm: {:3.3f}"
                  "| acc_nsp: {:3.3f}".format(
                    steps, 
                    total_steps, 
                    done_tokens / elapsed, 
                    loss, 
                    loss_mlm,
                    loss_nsp,
                    total_correct_mlm / total_denominator,
                    total_correct_nsp  / total_instances))
            
            done_tokens = 0
            total_loss, total_loss_mlm, total_loss_nsp = 0., 0., 0.
            total_correct_mlm, total_denominator = 0., 0.
            total_correct_nsp, total_instances = 0., 0.

            start_time = time.time()

        if steps % args.save_checkpoint_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            save_model(model, args.output_model_path + "-" + str(steps))

        steps += 1


def train_mlm(args, gpu_id, rank, loader, model, optimizer, scheduler):
    model.train()
    start_time = time.time()
    total_loss, total_loss_mlm, total_loss_nsp = 0., 0., 0.
    # Calculate MLM accuracy.
    total_correct, total_denominator = 0., 0. 
    steps = 1
    total_steps = args.total_steps
    loader_iter = iter(loader)

    while True:
        if steps == total_steps + 1:
            break
        src, tgt, seg = next(loader_iter)

        if gpu_id is not None:
            src = src.cuda(gpu_id)
            tgt = tgt.cuda(gpu_id)
            seg = seg.cuda(gpu_id)
        
        # Forward.
        loss_info = model(src, tgt, seg)
        loss, correct, denominator = loss_info
        
        # Backward.
        total_loss += loss.item()
        total_correct += correct.item()
        total_denominator += denominator.item()

        loss = loss / args.accumulation_steps

        if args.fp16:
            with args.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if steps % args.accumulation_steps == 0:
            optimizer.step()
            scheduler.step()
            model.zero_grad()
        
        if steps % args.report_steps == 0  and \
            (not args.dist_train or (args.dist_train and rank == 0)):

            loss = total_loss / args.report_steps

            elapsed = time.time() - start_time

            done_tokens = \
                args.batch_size * src.size(1) * args.report_steps * args.world_size \
                if args.dist_train \
                else args.batch_size * src.size(1) * args.report_steps

            print("| {:8d}/{:8d} steps"
                  "| {:8.2f} tokens/s"
                  "| loss {:7.2f}"
                  "| acc: {:3.3f}".format(
                    steps, 
                    total_steps, 
                    done_tokens / elapsed, 
                    loss, 
                    total_correct / total_denominator))
            
            total_loss = 0.
            total_correct, total_denominator = 0., 0.

            start_time = time.time()

        if steps % args.save_checkpoint_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            save_model(model, args.output_model_path + "-" + str(steps))

        steps += 1


def train_albert(args, gpu_id, rank, loader, model, optimizer, scheduler):
    model.train()
    start_time = time.time()
    total_loss, total_loss_mlm, total_loss_sop = 0., 0., 0.
    # Calculate MLM accuracy.
    total_correct_mlm, total_denominator = 0., 0. 
    # Calculate SOP accuracy.
    total_correct_sop, total_instances = 0., 0.
    steps = 1
    total_steps = args.total_steps
    done_tokens = 0
    loader_iter = iter(loader)

    while True:
        if steps == total_steps + 1:
            break
        src, tgt_mlm, tgt_sop, seg = next(loader_iter)

        if gpu_id is not None:
            src = src.cuda(gpu_id)
            tgt_mlm = tgt_mlm.cuda(gpu_id)
            tgt_sop = tgt_sop.cuda(gpu_id)
            seg = seg.cuda(gpu_id)
        
        # Forward.
        loss_info = model(src, (tgt_mlm, tgt_sop), seg)
        loss_mlm, loss_sop, correct_mlm, correct_sop, denominator = loss_info
        
         # Backward.
        loss = loss_mlm + loss_sop
        total_loss += loss.item()
        total_loss_mlm += loss_mlm.item()
        total_loss_sop += loss_sop.item()
        total_correct_mlm += correct_mlm.item()
        total_correct_sop += correct_sop.item()
        total_denominator += denominator.item()
        total_instances += src.size(0)
        done_tokens += src.size(0) * src.size(1)

        loss = loss / args.accumulation_steps

        if args.fp16:
            with args.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if steps % args.accumulation_steps == 0:
            optimizer.step()
            scheduler.step()
            model.zero_grad()
        
        if steps % args.report_steps == 0  and \
            (not args.dist_train or (args.dist_train and rank == 0)):

            loss = total_loss / args.report_steps
            loss_mlm = total_loss_mlm / args.report_steps
            loss_sop = total_loss_sop / args.report_steps

            elapsed = time.time() - start_time

            if args.dist_train:
                done_tokens *= args.world_size

            print("| {:8d}/{:8d} steps"
                  "| {:8.2f} tokens/s"
                  "| loss {:7.2f}"
                  "| loss_mlm: {:3.3f}"
                  "| loss_sop: {:3.3f}"
                  "| acc_mlm: {:3.3f}"
                  "| acc_sop: {:3.3f}".format(
                    steps, 
                    total_steps, 
                    done_tokens / elapsed, 
                    loss, 
                    loss_mlm,
                    loss_sop,
                    total_correct_mlm / total_denominator,
                    total_correct_sop  / total_instances))
            
            done_tokens = 0
            total_loss, total_loss_mlm, total_loss_sop = 0., 0., 0.
            total_correct_mlm, total_denominator = 0., 0.
            total_correct_sop, total_instances = 0., 0.

            start_time = time.time()

        if steps % args.save_checkpoint_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            save_model(model, args.output_model_path + "-" + str(steps))

        steps += 1


def train_lm(args, gpu_id, rank, loader, model, optimizer, scheduler):
    model.train()
    start_time = time.time()
    total_loss = 0.
    total_correct, total_denominator = 0., 0. 
    steps = 1
    total_steps = args.total_steps
    loader_iter = iter(loader)

    while True:
        if steps == total_steps + 1:
            break
        src, tgt, seg = next(loader_iter)

        if gpu_id is not None:
            src = src.cuda(gpu_id)
            tgt = tgt.cuda(gpu_id)
            seg = seg.cuda(gpu_id)
        
        # Forward.
        loss_info = model(src, tgt, seg)
        loss, correct, denominator = loss_info
        
        # Backward.
        total_loss += loss.item()
        total_correct += correct.item()
        total_denominator += denominator.item()

        loss = loss / args.accumulation_steps

        if args.fp16:
            with args.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if steps % args.accumulation_steps == 0:
            optimizer.step()
            scheduler.step()
            model.zero_grad()
        
        if steps % args.report_steps == 0  and \
            (not args.dist_train or (args.dist_train and rank == 0)):

            loss = total_loss / args.report_steps

            elapsed = time.time() - start_time

            done_tokens = \
                args.batch_size * src.size(1) * args.report_steps * args.world_size \
                if args.dist_train \
                else args.batch_size * src.size(1) * args.report_steps

            print("| {:8d}/{:8d} steps"
                  "| {:8.2f} tokens/s"
                  "| loss {:7.2f}"
                  "| acc: {:3.3f}".format(
                    steps, 
                    total_steps, 
                    done_tokens / elapsed, 
                    loss, 
                    total_correct / total_denominator))
            
            total_loss = 0.
            total_correct, total_denominator = 0., 0.

            start_time = time.time()

        if steps % args.save_checkpoint_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            save_model(model, args.output_model_path + "-" + str(steps))

        steps += 1


def train_bilm(args, gpu_id, rank, loader, model, optimizer, scheduler):
    model.train()
    start_time = time.time()
    total_loss, total_loss_forward, total_loss_backward = 0., 0., 0.
    total_correct_forward, total_correct_backward, total_denominator = 0., 0., 0. 
    steps = 1
    total_steps = args.total_steps
    loader_iter = iter(loader)

    while True:
        if steps == total_steps + 1:
            break
        src, tgt_forward, tgt_backward, seg = next(loader_iter)

        if gpu_id is not None:
            src = src.cuda(gpu_id)
            tgt_forward = tgt_forward.cuda(gpu_id)
            tgt_backward = tgt_backward.cuda(gpu_id)
            seg = seg.cuda(gpu_id)
        
        # Forward.
        loss_info = model(src, (tgt_forward, tgt_backward), seg)
        loss_forward, loss_backward, correct_forward, correct_backward, denominator = loss_info
        
        # Backward.
        loss = loss_forward + loss_backward
        total_loss += loss.item()
        total_loss_forward += loss_forward.item()
        total_loss_backward += loss_backward.item()
        total_correct_forward += correct_forward.item()
        total_correct_backward += correct_backward.item()
        total_denominator += denominator.item()

        loss = loss / args.accumulation_steps

        if args.fp16:
            with args.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if steps % args.accumulation_steps == 0:
            optimizer.step()
            scheduler.step()
            model.zero_grad()
        
        if steps % args.report_steps == 0  and \
            (not args.dist_train or (args.dist_train and rank == 0)):

            loss = total_loss / args.report_steps

            elapsed = time.time() - start_time

            done_tokens = \
                args.batch_size * src.size(1) * args.report_steps * args.world_size \
                if args.dist_train \
                else args.batch_size * src.size(1) * args.report_steps

            print("| {:8d}/{:8d} steps"
                  "| {:8.2f} tokens/s"
                  "| loss {:7.2f}"
                  "| loss_forward {:3.3f}"
                  "| loss_backward {:3.3f}"
                  "| acc_forward: {:3.3f}"
                  "| acc_backward: {:3.3f}".format(
                    steps, 
                    total_steps, 
                    done_tokens / elapsed, 
                    loss,
                    loss_forward,
                    loss_backward,
                    total_correct_forward / total_denominator,
                    total_correct_backward / total_denominator))
            
            total_loss, total_loss_forward, total_loss_backward = 0., 0., 0.
            total_correct_forward, total_correct_backward, total_denominator = 0., 0., 0. 

            start_time = time.time()

        if steps % args.save_checkpoint_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            save_model(model, args.output_model_path + "-" + str(steps))

        steps += 1


def train_cls(args, gpu_id, rank, loader, model, optimizer, scheduler):
    model.train()
    start_time = time.time()
    total_loss = 0.
    total_correct, total_instances = 0., 0.
    steps = 1
    total_steps = args.total_steps
    loader_iter = iter(loader)

    while True:
        if steps == total_steps + 1:
            break
        src, tgt, seg = next(loader_iter)

        if gpu_id is not None:
            src = src.cuda(gpu_id)
            tgt = tgt.cuda(gpu_id)
            seg = seg.cuda(gpu_id)

        #         # Forward.
        loss_info = model(src, tgt, seg)
        loss, correct = loss_info

        #         # Backward.
        total_loss += loss.item()
        total_correct += correct.item()
        total_instances += src.size(0)

        loss = loss / args.accumulation_steps

        if args.fp16:
            with args.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if steps % args.accumulation_steps == 0:
            optimizer.step()
            scheduler.step()
            model.zero_grad()

        if steps % args.report_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            loss = total_loss / args.report_steps

            elapsed = time.time() - start_time

            done_tokens = \
                args.batch_size * src.size(1) * args.report_steps * args.world_size \
                    if args.dist_train \
                    else args.batch_size * src.size(1) * args.report_steps

            print("| {:8d}/{:8d} steps"
                  "| {:8.2f} tokens/s"
                  "| loss {:7.2f}"
                  "| acc: {:3.3f}".format(
                steps,
                total_steps,
                done_tokens / elapsed,
                loss,
                total_correct / total_instances))

            total_loss = 0.
            total_correct = 0.
            total_instances = 0.

            start_time = time.time()

        if steps % args.save_checkpoint_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            save_model(model, args.output_model_path + "-" + str(steps))

        steps += 1


def train_mt(args, gpu_id, rank, loader, model, optimizer, scheduler):
    model.train()
    start_time = time.time()
    total_loss = 0.
    total_correct, total_denominator = 0., 0.
    steps = 1
    total_steps = args.total_steps
    loader_iter = iter(loader)

    while True:
        if steps == total_steps + 1:
            break
        src, tgt_in, tgt_out, seg = next(loader_iter)
        if gpu_id is not None:
            src = src.cuda(gpu_id)
            tgt_in = tgt_in.cuda(gpu_id)
            tgt_out = tgt_out.cuda(gpu_id)
            seg = seg.cuda(gpu_id)
        # Forward.
        loss_info = model(src, (tgt_in, tgt_out, src), seg)
        loss, correct, denominator = loss_info
        # Backward.
        total_loss += loss.item()
        total_correct += correct.item()
        total_denominator += denominator.item()

        loss = loss / args.accumulation_steps

        if args.fp16:
            with args.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if steps % args.accumulation_steps == 0:
            optimizer.step()
            scheduler.step()
            model.zero_grad()

        if steps % args.report_steps == 0  and \
            (not args.dist_train or (args.dist_train and rank == 0)):

            loss = total_loss / args.report_steps

            elapsed = time.time() - start_time

            done_tokens = \
                args.batch_size * src.size(1) * args.report_steps * args.world_size \
                if args.dist_train \
                else args.batch_size * src.size(1) * args.report_steps

            print("| {:8d}/{:8d} steps"
                  "| {:8.2f} tokens/s"
                  "| loss {:7.2f}"
                  "| acc: {:3.3f}".format(
                    steps,
                    total_steps,
                    done_tokens / elapsed,
                    loss,
                    total_correct / total_denominator))

            total_loss = 0.
            total_correct, total_denominator = 0., 0.

            start_time = time.time()

        if steps % args.save_checkpoint_steps == 0 and \
                (not args.dist_train or (args.dist_train and rank == 0)):
            save_model(model, args.output_model_path + "-" + str(steps))

        steps += 1 



str2trainer = {"bert": train_bert, "lm": train_lm, "mlm": train_mlm,
               "bilm": train_bilm, "albert": train_albert, "mt": train_mt,
               "t5": train_mt, "cls": train_cls}

def worker(proc_id, gpu_ranks, args, model):
    """
    Args:
        proc_id: The id of GPU for single GPU mode;
                 The id of process (and GPU) for multiprocessing distributed mode.
        gpu_ranks: List of ranks of each process.
    """
    set_seed(args.seed)

    if args.dist_train:
        rank = gpu_ranks[proc_id]
        gpu_id = proc_id
    elif args.single_gpu:
        rank = None
        gpu_id = proc_id
    else:
        rank = None
        gpu_id = None

    if args.dist_train:
        train_loader = str2dataloader[args.target](args, args.dataset_path, args.batch_size, rank, args.world_size, True)
    else:
        train_loader = str2dataloader[args.target](args, args.dataset_path, args.batch_size, 0, 1, True)

    if gpu_id is not None:
        torch.cuda.set_device(gpu_id)
        model.cuda(gpu_id)

    # Build optimizer.
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay_rate': 0.0}
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, betas=(args.beta1, args.beta2), correct_bias=False)
    scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.total_steps*args.warmup, t_total=args.total_steps)

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)
        args.amp = amp

    if args.dist_train:
        # Initialize multiprocessing distributed training environment.
        dist.init_process_group(backend=args.backend,
                                init_method=args.master_ip,
                                world_size=args.world_size,
                                rank=rank)
        model = DistributedDataParallel(model, device_ids=[gpu_id])
        print("Worker %d is training ... " % rank)
    else:
        print("Worker is training ...")

    str2trainer[args.target](args, gpu_id, rank, train_loader, model, optimizer, scheduler)



# def train_nsp(args, gpu_id, rank, loader, model, optimizer):
#     model.train()
#     start_time = time.time()
#     total_loss = 0.
#     total_correct, total_instances = 0., 0.
#     steps = 1
#     total_steps = args.total_steps
#     loader_iter = iter(loader)

#     while True:
#         if steps == total_steps + 1:
#             break
#         src, tgt, seg = next(loader_iter)

#         if gpu_id is not None:
#             src = src.cuda(gpu_id)
#             tgt = tgt.cuda(gpu_id)
#             seg = seg.cuda(gpu_id)
        
#         # Forward.
#         loss_info = model(src, tgt, seg)
#         loss, correct = loss_info
        
#         # Backward.
#         total_loss += loss.item()
#         total_correct += correct.item()
#         total_instances += src.size(0)

#         loss = loss / args.accumulation_steps
#         loss.backward()

#         if steps % args.accumulation_steps == 0:
#             optimizer.step()
#             model.zero_grad()
        
#         if steps % args.report_steps == 0  and \
#             (not args.dist_train or (args.dist_train and rank == 0)):

#             loss = total_loss / args.report_steps

#             elapsed = time.time() - start_time

#             done_tokens = \
#                 args.batch_size * src.size(1) * args.report_steps * args.world_size \
#                 if args.dist_train \
#                 else args.batch_size * src.size(1) * args.report_steps

#             print("| {:8d}/{:8d} steps"
#                   "| {:8.2f} tokens/s"
#                   "| loss {:7.2f}"
#                   "| acc: {:3.3f}".format(
#                     steps, 
#                     total_steps, 
#                     done_tokens / elapsed, 
#                     loss, 
#                     total_correct / total_instances))
            
#             total_loss = 0.
#             total_correct = 0.
#             total_instances = 0.

#             start_time = time.time()

#         if steps % args.save_checkpoint_steps == 0 and \
#                 (not args.dist_train or (args.dist_train and rank == 0)):
#             save_model(model, args.output_model_path + "-" + str(steps))

#         steps += 1


# def train_s2s(args, gpu_id, rank, loader, model, optimizer):
#     model.train()
#     start_time = time.time()
#     total_loss= 0.
#     total_correct, total_denominator = 0., 0. 
#     steps = 1
#     total_steps = args.total_steps
#     loader_iter = iter(loader)

#     while True:
#         if steps == total_steps + 1:
#             break
#         src, tgt, seg = next(loader_iter)

#         if gpu_id is not None:
#             src = src.cuda(gpu_id)
#             tgt = tgt.cuda(gpu_id)
#             seg = seg.cuda(gpu_id)
        
#         # Forward.
#         loss_info = model(src, tgt, seg)
#         loss, correct, denominator = loss_info
        
#         # Backward.
#         total_loss += loss.item()
#         total_correct += correct.item()
#         total_denominator += denominator.item()

#         loss = loss / args.accumulation_steps
#         loss.backward()

#         if steps % args.accumulation_steps == 0:
#             optimizer.step()
#             model.zero_grad()
        
#         if steps % args.report_steps == 0  and \
#             (not args.dist_train or (args.dist_train and rank == 0)):

#             loss = total_loss / args.report_steps

#             elapsed = time.time() - start_time

#             done_tokens = \
#                 args.batch_size * src.size(1) * args.report_steps * args.world_size \
#                 if args.dist_train \
#                 else args.batch_size * src.size(1) * args.report_steps

#             print("| {:8d}/{:8d} steps"
#                   "| {:8.2f} tokens/s"
#                   "| loss {:7.2f}"
#                   "| acc: {:3.3f}".format(
#                     steps, 
#                     total_steps, 
#                     done_tokens / elapsed, 
#                     loss, 
#                     total_correct / total_denominator))
            
#             total_loss = 0.
#             total_correct, total_denominator = 0., 0.

#             start_time = time.time()

#         if steps % args.save_checkpoint_steps == 0 and \
#                 (not args.dist_train or (args.dist_train and rank == 0)):
#             save_model(model, args.output_model_path + "-" + str(steps))

#         steps += 1
