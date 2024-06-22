# -*- coding: utf-8 -*-
# @Author  : jingyi


import time

import wandb
from tqdm import tqdm
from collections import defaultdict

import torch
import os
import numpy as np
from util.smpl import get_smpl_vertices, SMPL, save_ply
from util.utils import output_metric


def mean(lis): return sum(lis) / len(lis)


class Trainer():
    def __init__(self, net, loader, loss, cfg, gpu, eval=False):
        self.generator = net
        self.loader = loader
        self.loss_func = loss
        self.gpu = gpu
        #self.gen_optimizer = torch.optim.Adam([p for p in net.parameters() if p.requires_grad],lr=cfg.TRAIN.GEN.LR, weight_decay=cfg.TRAIN.GEN.WD)
        self.eval = eval
        if not eval:
            self.gen_optimizer = torch.optim.Adam(lr=cfg.TRAIN['GEN']['LR'], params=net.parameters(),
                                                  weight_decay=cfg.TRAIN['GEN']['WD'])
            sc = cfg.TRAIN['GEN']

            if cfg.TRAIN['CosineAnnealingWarmRestarts']:
                self.lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(self.gen_optimizer, T_0=10, T_mult=2, eta_min=sc['min_lr'])
            else:
                self.lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                    self.gen_optimizer, 'min', factor=sc['factor'], patience=sc['patience'],
                    threshold_mode='rel', threshold=sc['threshold'], min_lr=sc['min_lr'],
                    verbose=True)
            self.lr_scheduler_metrics = cfg.TRAIN['GEN']['metrics']
        self.cfg = cfg

    def todevice(self, x, gpu):
        if isinstance(x, dict):
            return {k: self.todevice(v, gpu) for k, v in x.items()}
        if isinstance(x, (tuple, list)):
            return [self.todevice(v, gpu) for v in x]

        if True:
            # 如果网络在 cuda 里面, 则将数据 contiguous 化,
            # non_blocking 是从 cpu 拷贝到 gpu 可能可以异步进行
            if isinstance(x, str):
                return x
            else:
                # return x.contiguous().cuda(gpu,non_blocking=True)
                return x.contiguous().to(gpu, non_blocking=True)
        else:
            return x.cpu()

    def train(self, epoch: int):
        self.generator.train()

        # assert torch.is_grad_enabled() == istrain, '"grad_enabled" is differ "istrain"'
        stats = defaultdict(list)  # 简单来看, 当作一个 dictionary 即可

        loader = self.loader['Train']

        if self.cfg.TRAIN['use_drop_first']:
            loader.dataset.update_cfg(drop_first_n=np.random.randint(0, 16))

        bar = tqdm(loader, ncols=60)
        bar.set_description(f'Train {epoch:02d}')
        for bi, inputs in enumerate(bar):
            #torch.cuda.empty_cache()
            inputs = self.todevice(inputs, self.gpu)

            output = self.generator(inputs)
            loss_dict, others= self.loss_func(output)

            self.gen_optimizer.zero_grad()
            loss_dict['loss'].backward()
            self.gen_optimizer.step()

            for k, v in loss_dict.items():
                stats[k].append(v.detach().cpu())

            wandb.log({'train': loss_dict}, step=wandb.run.step + loader.batch_size // 2)

            if self.cfg.TRAIN['CosineAnnealingWarmRestarts']:
                self.lr_scheduler.step(epoch + bi / len(loader))
            #del inputs, output, loss_dict, others
            #break
            #if bi > 10:
            #    break

        loss_summary = {'mean_' + k: torch.tensor(v).mean() for k, v in stats.items()}
        wandb.log({'train': loss_summary})

        del stats, inputs, output, loss_dict

        return loss_summary

    def evaluate(self, epoch=0, log_table=None):
        all_pred_joints = []
        all_gt_joints = []
        
        stats = defaultdict(list)

        self.generator.eval()
        test_loader = self.loader['Valid']

        bar = tqdm(test_loader, ncols=60)
        bar.set_description(f'eval {epoch:02d}')
        for index, inputs in enumerate(bar):
            with torch.no_grad():
                inputs = {k: v.to(self.gpu, non_blocking=True) for k, v in inputs.items()}

                output = self.generator(inputs)

                loss_dict, others = self.loss_func(output)

                for k, v in loss_dict.items():
                    stats[k].append(v.detach().cpu())

            wandb.log({'eval': loss_dict}, step=wandb.run.step + test_loader.batch_size // 2)
            
            pred_joints = others['pred_smpl_joints'].reshape(-1, 24, 3).cpu().numpy()
            gt_joints = output['full_joints'].reshape(-1, 24, 3).cpu().numpy()
            all_pred_joints.append(pred_joints)
            all_gt_joints.append(gt_joints)
            
        all_pred_joints = np.concatenate(all_pred_joints, axis=0)
        all_gt_joints = np.concatenate(all_gt_joints, axis=0)

        metric = output_metric(all_pred_joints, all_gt_joints)

        loss_summary = {'mean_' + k: torch.tensor(v).mean() for k, v in stats.items()}
        loss_summary.update(metric)
        wandb.log({'eval': loss_summary})

        raw_mpjpe = [epoch, ]
        for dataset_id, pred_joints, gt_joints in test_loader.dataset.split_list_by_dataset(
                all_pred_joints.reshape(-1, 24, 3),
                all_gt_joints.reshape(-1, 24, 3)):
            metric = output_metric(pred_joints.reshape(-1, 24, 3),
                                       gt_joints.reshape(-1, 24, 3))
            print(dataset_id, metric)
            raw_mpjpe.append(metric['mpjpe'])
        log_table.add_data(*raw_mpjpe)
        wandb.log(
            {"eval_mpjpe": wandb.Table(columns=log_table.columns, rows=log_table.data)})
        wandb.log({"eval_best_mpjpe": wandb.Table(columns=log_table.columns,
                                                  rows=np.array(log_table.data).min(
                                                      axis=0)[np.newaxis, :].tolist())})

        loss_summary = {'mean_' + k: torch.tensor(v).mean() for k, v in stats.items()}
        wandb.log({'eval': loss_summary})

        del stats, inputs, output, loss_dict, pred_joints, gt_joints, all_pred_joints, \
            all_gt_joints, metric, raw_mpjpe
        
        return loss_summary

    def fit(self, start_epoch, end_epoch, dir):
        log_table = wandb.Table(columns=['epoch', ] + [str(e) for e in self.loader['Valid'].dataset.dataset_ids])

        if self.eval:
            eval_summary = self.evaluate()

        else:
            best_performance = float('inf')
            for epoch in range(start_epoch, end_epoch):
                wandb.log({'train': {'gen_lr': self.gen_optimizer.param_groups[0]['lr']}})
                torch.cuda.empty_cache()
                train_summary = self.train(epoch)

                eval_summary = self.evaluate(epoch=epoch, log_table=log_table)

                if self.lr_scheduler is not None and not self.cfg.TRAIN['CosineAnnealingWarmRestarts']:
                    m1, m2 = self.lr_scheduler_metrics.split('.')
                    performance = train_summary[m2] if m1 == 'train' else eval_summary[m2]
                    print(f'lr_sceduler metric:{performance}')
                    self.lr_scheduler.step(performance)

                if eval_summary['mean_loss'] < best_performance:
                    best_performance = eval_summary['mean_loss']
                    self.save_model(eval_summary, epoch, os.path.join(dir, 'best_model.pth'))
                self.save_model(eval_summary, epoch, os.path.join(dir, 'lasted_model.pth'))
        del train_summary, eval_summary

    def save_model(self, performance, epoch, filename):
        save_dict = {
            'epoch': epoch,
            'gen_state_dict': self.generator.state_dict(),
            'performance': performance,
            'gen_optimizer': self.gen_optimizer.state_dict(),
        }

        torch.save(save_dict, filename)

        print(f"Save model at:'{filename}'")

    def load_model(self, model_path):
        if os.path.isfile(model_path):
            checkpoint = torch.load(model_path, map_location='cuda:{}'.format(self.gpu))
            model_dict = self.generator.state_dict()
            pretrain_list = checkpoint['gen_state_dict'] if 'gen_state_dict' in checkpoint else checkpoint['state_dict']
            model_list = list(model_dict.keys())
            pretrain_model = {k: v for k, v in pretrain_list.items() if k in model_dict}
            model_dict.update(pretrain_model)
            self.generator.load_state_dict(model_dict)

            return self.generator
    

    def resume_pretrained(self, model_path):
        if os.path.isfile(model_path):
            checkpoint = torch.load(model_path, map_location='cuda:{}'.format(self.gpu))
            model_dict = self.generator.state_dict()
            pretrain_list = checkpoint['gen_state_dict'] if 'gen_state_dict' in checkpoint else checkpoint['state_dict']
            model_list = list(model_dict.keys())
            pretrain_model = {k: v for k, v in pretrain_list.items() if k in model_dict}
            model_dict.update(pretrain_model)
            self.generator.load_state_dict(model_dict)

            # if not self.visual:
            #if 'gen_optimizer' in checkpoint:
            #    self.gen_optimizer.load_state_dict(checkpoint['gen_optimizer'])

            start_epoch = checkpoint['epoch'] if 'epoch' in checkpoint else 1
            # 
            best_performance = checkpoint['performance']if 'performance' in checkpoint else float('inf')
            # 
            # print(f"=> loaded checkpoint '{model_path}' ")
            return start_epoch, best_performance
        else:
            print(f"=> no checkpoint found at '{model_path}'")


class Evaler():
    def __init__(self, net, loader, loss, cfg, gpu, eval=False):
        self.generator = net
        self.loader = loader
        self.loss_func = loss
        self.gpu = gpu
        # self.gen_optimizer = torch.optim.Adam([p for p in net.parameters() if p.requires_grad],lr=cfg.TRAIN.GEN.LR, weight_decay=cfg.TRAIN.GEN.WD)
        self.eval = eval
        self.cfg = cfg

    def todevice(self, x, gpu):
        if isinstance(x, dict):
            return {k: self.todevice(v, gpu) for k, v in x.items()}
        if isinstance(x, (tuple, list)):
            return [self.todevice(v, gpu) for v in x]

        if True:
            # 如果网络在 cuda 里面, 则将数据 contiguous 化,
            # non_blocking 是从 cpu 拷贝到 gpu 可能可以异步进行
            if isinstance(x, str):
                return x
            else:
                # return x.contiguous().cuda(gpu,non_blocking=True)
                return x.contiguous().to(gpu, non_blocking=True)
        else:
            return x.cpu()

    def evaluate(self, epoch=0, log_table=None):
        stats = defaultdict(list)
        
        all_pred_joints = []
        all_gt_joints = []
        all_pred_pose = []
        all_gt_pose = []
        all_pc = []
        all_trans = []
        all_outlier = []

        self.generator = self.load_model(
            os.path.join('output',
                         self.cfg.ckpt_path, 'best_model.pth'))

        self.generator.eval()
        test_loader = self.loader['Valid']

        bar = tqdm(test_loader, ncols=60)
        bar.set_description(f'eval {epoch:02d}')
        for index, inputs in enumerate(bar):
            with torch.no_grad():
                inputs = {k: v.to(self.gpu, non_blocking=True) for k, v in
                          inputs.items()}

                output = self.generator(inputs)
                
                from scipy.spatial.transform import Rotation as R
                # pred_pose = output['pred_rotmats'].reshape(-1,3,3) # (B * T * 24, 3, 3)
                # _pred_pose = rotation_matrix_to_axis_angle(pred_pose) # (B * T * 24, 3)
                # _pred_pose = _pred_pose.reshape(-1,24,3)
                # all_pred_pose.append(_pred_pose.reshape(-1,72).cpu().numpy())
                # all_gt_pose.append(output['pose'].reshape(-1,72).cpu().numpy())
                gt_pose = (R.from_matrix(output['rotmats'].cpu().reshape(-1, 3, 3))).\
                    as_rotvec().reshape(output['rotmats'].shape[0], output['rotmats'].shape[1], 72)
                pred_pose = (R.from_matrix(output['pred_rotmats'].cpu().reshape(-1, 3,3)))\
                    .as_rotvec().reshape(output['rotmats'].shape[0], output['rotmats'].shape[1], 72)
                pc = (output['human_points'] + output['trans'].unsqueeze(-2)).cpu().numpy()
                all_pred_pose.append(pred_pose)
                all_gt_pose.append(gt_pose)
                all_pc.append(pc)
                all_trans.append(output['trans'].cpu().numpy())

                # ------------ visual test --------------
                # pred_rotmats = output['pred_rotmats']
                # smpl = SMPL().cuda()
                # B, T, _, _ = output['human_points'].shape
                # 
                # pred_vertices = get_smpl_vertices(output['trans'].reshape(
                #     B * T, 3), pred_rotmats.reshape(B * T, 24, 3, 3),
                #     output['betas'].reshape(B * T, 10), smpl)
                # 
                # for index in range(pred_vertices.shape[0]):
                #     save_ply(pred_vertices[index], os.path.join(
                #         './visual_test', '{}.ply'.format(index)))
                # 
                # true_vertices = get_smpl_vertices(output['trans'].reshape(
                #     B * T, 3), output['rotmats'].reshape(B * T, 24, 3, 3),
                #     output['betas'].reshape(B * T, 10), smpl)
                # 
                # for index in range(true_vertices.shape[0]):
                #     save_ply(true_vertices[index], os.path.join(
                #         './visual_test/true_pose', '{}.ply'.format(index)))
                # 
                # human_points = output['human_points'].squeeze().cpu().numpy()
                # for index in range(human_points.shape[0]):
                #     np.savetxt(os.path.join('./visual_test', '{}.txt'.format(index)),
                #                human_points[index])

                # human_points = output['human_points']
                # target_pc = output['sample_pc']
                # # nn_target = output['gt_tar']
                # pred = output['predict_normal_pc']
                # np.savetxt('pred.txt', pred.detach().cpu().numpy()[0][0])
                # np.savetxt('target.txt', target_pc.cpu().numpy()[0][0])
                # # np.savetxt('nn_target.txt', nn_target.cpu().numpy()[0][0])
                # np.savetxt('ori.txt', human_points.cpu().numpy()[0][0])
                # break
                #

                loss_dict, others = self.loss_func(output)

                for k, v in loss_dict.items():
                    stats[k].append(v.detach().cpu())
            
            wandb.log({'eval': loss_dict},
                      step=wandb.run.step + test_loader.batch_size // 2)

            pred_joints = others['pred_smpl_joints'].reshape(-1, 24, 3).cpu().numpy()
            gt_joints = output['full_joints'].reshape(-1, 24, 3).cpu().numpy()
            all_pred_joints.append(pred_joints)
            all_gt_joints.append(gt_joints)

        all_pred_joints = np.concatenate(all_pred_joints, axis=0)
        all_gt_joints = np.concatenate(all_gt_joints, axis=0)
            
        all_pred_pose = np.concatenate(all_pred_pose, axis=0)
        all_gt_pose = np.concatenate(all_gt_pose, axis=0)
        all_pc = np.concatenate(all_pc, axis=0)
        all_trans = np.concatenate(all_trans, axis=0)
        
        metric = output_metric(all_pred_joints, all_gt_joints)
        for k, v in metric.items():
            print(f'{k}:{v}')
            
        del inputs, output, loss_dict

        loss_summary = {'mean_' + k: torch.tensor(v).mean() for k, v in stats.items()}
        wandb.log({'eval': loss_summary})

        del stats
        return loss_summary

    def fit(self, start_epoch, end_epoch, dir):
        log_table = wandb.Table(columns=['epoch', ] + [str(e) for e in self.loader[
            'Valid'].dataset.dataset_ids])

        eval_summary = self.evaluate()

    def save_model(self, performance, epoch, filename):
        save_dict = {
            'epoch': epoch,
            'gen_state_dict': self.generator.state_dict(),
            'performance': performance,
            'gen_optimizer': self.gen_optimizer.state_dict(),
        }

        torch.save(save_dict, filename)

        print(f"Save model at:'{filename}'")

    def load_model(self, model_path):
        if os.path.isfile(model_path):
            checkpoint = torch.load(model_path, map_location='cuda:{}'.format(self.gpu))
            model_dict = self.generator.state_dict()
            pretrain_list = checkpoint[
                'gen_state_dict'] if 'gen_state_dict' in checkpoint else checkpoint[
                'state_dict']
            model_list = list(model_dict.keys())
            pretrain_model = {k: v for k, v in pretrain_list.items() if k in model_dict}
            model_dict.update(pretrain_model)
            self.generator.load_state_dict(model_dict)

            return self.generator



