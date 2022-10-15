import argparse
from tqdm import tqdm
import math
import torch
import torchvision as tv
import wandb
from timm.utils import unwrap_model

# import mycv.utils.loggers as mylog
# from mycv.training import IterTrainWrapper
# from mycv.utils.ddp import check_model_equivalence
# from mycv.datasets.compression import get_bd_rate_over_anchor
# from mycv.datasets.imgen import get_dateset, datasets_root
from lvae.trainer import BaseTrainingWrapper


def parse_args():
    # ====== set the run settings ======
    parser = argparse.ArgumentParser()
    # wandb setting
    parser.add_argument('--wbproject',  type=str,  default='default')
    parser.add_argument('--wbgroup',    type=str,  default='var-rate-exp')
    parser.add_argument('--wbtags',     type=str,  default=None, nargs='+')
    parser.add_argument('--wbnote',     type=str,  default=None)
    parser.add_argument('--wbmode',     type=str,  default='disabled')
    parser.add_argument('--name',       type=str,  default=None)
    # model setting
    parser.add_argument('--model',      type=str,  default='mb128_var_depth')
    parser.add_argument('--model_args', type=str,  default='')
    # resume setting
    parser.add_argument('--resume',     type=str,  default=None)
    parser.add_argument('--weights',    type=str,  default=None)
    parser.add_argument('--load_optim', action=argparse.BooleanOptionalAction, default=False)
    # data setting
    parser.add_argument('--trainset',   type=str,  default='coco_train2017')
    parser.add_argument('--transform',  type=str,  default='crop=256,hflip=True')
    parser.add_argument('--valset',     type=str,  default='kodak')
    parser.add_argument('--val_steps',  type=int,  default=8)
    # parser.add_argument('--val_bs',     type=int,  default=None)
    # optimization setting
    parser.add_argument('--batch_size', type=int,  default=16)
    parser.add_argument('--accum_num',  type=int,  default=1)
    parser.add_argument('--optimizer',  type=str,  default='adam')
    parser.add_argument('--lr',         type=float,default=2e-4)
    parser.add_argument('--lr_sched',   type=str,  default='const-0.75-cos')
    parser.add_argument('--lrf_min',    type=float,default=0.01)
    parser.add_argument('--lr_warmup',  type=int,  default=0)
    parser.add_argument('--grad_clip',  type=float,default=2.0)
    # training iterations setting
    parser.add_argument('--iterations', type=int,  default=2_000_000)
    parser.add_argument('--log_itv',    type=int,  default=100)
    parser.add_argument('--study_itv',  type=int,  default=2000)
    # parser.add_argument('--save_per',   type=int,  default=1000)
    parser.add_argument('--eval_itv',   type=int,  default=4000)
    parser.add_argument('--eval_first', action=argparse.BooleanOptionalAction, default=True)
    # exponential moving averaging (EMA)
    parser.add_argument('--ema',        action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--ema_decay',  type=float,default=0.9999)
    parser.add_argument('--ema_warmup', type=int,  default=10_000)
    # device setting
    parser.add_argument('--fixseed',    action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--workers',    type=int,  default=0)
    cfg = parser.parse_args()

    cfg.wdecay = 0.0
    cfg.amp = False
    return cfg


def make_generator(dataset, batch_size, workers):
    from torch.utils.data import DataLoader
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=True,
                            num_workers=workers, pin_memory=True)
    while True:
        yield from dataloader


class TrainWrapper(BaseTrainingWrapper):
    model_registry_group = 'generative'

    def main(self, cfg):
        self.cfg = cfg

        # preparation
        self.set_logging()
        self.set_device()
        self.prepare_configs()
        if self.distributed:
            with ddputils.run_zero_first(): # training set
                self.set_dataset_()
            torch.distributed.barrier()
        else:
            self.set_dataset_()
        self.set_model_()
        self.set_optimizer_()
        self.set_pretrain()

        # logging
        self.ema = None
        if self.is_main:
            self.set_wandb_()
            self.set_ema_()
            header = ['Epoch', 'Iter', 'GPU_mem', 'lr', 'grad']
            self.stats_table = utils.SimpleTable(header)

        if self.distributed: # DDP mode
            self.model = DDP(self.model, device_ids=[self.local_rank], output_device=self.local_rank)

        # the main training loops
        self.training_loops()

    def prepare_configs(self):
        super().prepare_configs()
        cfg = self.cfg
        self.ddp_check_interval = cfg.eval_itv
        self.model_log_interval = cfg.study_itv
        self.wandb_log_interval = cfg.log_itv

    def set_dataset_(self):
        cfg = self.cfg

        mylog.info('Initializing Datasets and Dataloaders...')
        trainset = get_dateset(cfg.trainset, transform_cfg=cfg.transform)
        trainloader = make_generator(trainset, batch_size=cfg.batch_size, workers=cfg.workers)
        mylog.info(f'Training root: {trainset.root}')
        mylog.info(f'Number of training images = {len(trainset)}')
        mylog.info(f'Training transform: \n{str(trainset.transform)}')

        # test set
        val_img_dir = datasets_root[cfg.valset]
        mylog.info(f'Val root: {val_img_dir} \n')

        self._epoch_len  = len(trainset) / cfg.bs_effective
        self.trainloader = trainloader
        # self.valloader   = valloader
        self.val_img_dir = val_img_dir
        self.cfg.epochs  = float(cfg.iterations / self._epoch_len)

    def training_loops(self):
        cfg = self.cfg
        model = self.model

        # ======================== initialize logging ========================
        pbar = range(self._cur_iter, cfg.iterations)
        if self.is_main:
            pbar = tqdm(pbar)
            self.init_logging_(print_header=False)
        # ======================== start training ========================
        for step in pbar:
            self._cur_iter  = step
            self._cur_epoch = step / self._epoch_len

            # DDP sanity check
            if self.distributed and (step % self.ddp_check_interval == 0):
                # If DDP mode, synchronize model parameters on all gpus
                check_model_equivalence(model, log_path=self._log_dir/'ddp.txt')

            # evaluation
            if self.is_main:
                if cfg.eval_itv <= 0: # no evaluation
                    pass
                elif (step == 0) and (not cfg.eval_first): # first iteration
                    pass
                elif step % cfg.eval_itv == 0: # evaluate every {cfg.eval_itv} epochs
                    self.evaluate()
                    model.train()
                    print(self._pbar_header)

            # learning rate schedule
            if step % 10 == 0:
                self.adjust_lr_(step, cfg.iterations)
                # if hasattr(unwrap_model(model), 'set_training_stage'):
                #     unwrap_model(model).set_training_stage(step / cfg.iterations)

            # training step
            assert model.training
            batch = next(self.trainloader)
            stats = model(batch)
            loss = stats['loss'] / float(cfg.accum_num)
            loss.backward() # gradients are averaged over devices in DDP mode
            # parameter update
            if step % cfg.accum_num == 0:
                grad_norm, bad = self.gradient_clip_(model.parameters())
                self.optimizer.step()
                self.optimizer.zero_grad()

                if (self.ema is not None) and not bad:
                    _warmup = cfg.ema_warmup or (cfg.iterations // 20)
                    self.ema.decay = cfg.ema_decay * (1 - math.exp(-step / _warmup))
                    self.ema.update(model)

            # sanity check
            if torch.isnan(loss).any() or torch.isinf(loss).any():
                mylog.error(f'loss = {loss}')
                self.clean_and_exit()

            # logging
            if self.is_main:
                self.minibatch_log(pbar, stats)
                self.periodic_log(batch)

        self._cur_iter += 1
        if self.is_main:
            self.evaluate()
            mylog.info(f'Training finished. results: \n {self._results}')

    def periodic_log(self, batch):
        assert self.is_main
        # model logging
        if self._cur_iter % self.model_log_interval == 0:
            self.model.eval()
            model = unwrap_model(self.model)
            if hasattr(model, 'study'):
                model.study(save_dir=self._log_dir, wandb_run=self.wbrun)
                # self.ema.ema.study(save_dir=self._log_dir/'ema')
                self.ema.module.study(save_dir=self._log_dir/'ema')
            self.model.train()

        # Weights & Biases logging
        if self._cur_iter % self.wandb_log_interval == 0:
            imgs = batch if torch.is_tensor(batch) else batch[0]
            assert torch.is_tensor(imgs)
            N = min(16, imgs.shape[0])
            tv.utils.save_image(imgs[:N], fp=self._log_dir / 'inputs.png', nrow=math.ceil(N**0.5))

            _log_dic = {
                'general/lr': self.optimizer.param_groups[0]['lr'],
                # 'general/grad_norm': self._moving_max_grad_norm,
                'general/grad_norm': self._moving_grad_norm_buffer.max(),
                'ema/decay': (self.ema.decay if self.ema else 0)
            }
            _log_dic.update(
                {'train/'+k: self.stats_table[k] for k in self.wandb_log_keys}
            )
            self.wbrun.log(_log_dic, step=self._cur_iter)

    @torch.no_grad()
    def evaluate(self):
        assert self.is_main
        log_dir = self._log_dir

        # Evaluation
        _log_dic = {
            'general/epoch': self._cur_epoch,
            'general/iter':  self._cur_iter
        }
        model_ = unwrap_model(self.model).eval()
        results = model_.self_evaluate(self.val_img_dir, log_dir=log_dir, steps=self.cfg.val_steps)
        results_to_log = self.process_log_results(results)

        _log_dic.update({'val-metrics/plain-'+k: v for k,v in results_to_log.items()})
        # save last checkpoint
        checkpoint = {
            'model'     : model_.state_dict(),
            'optimizer' : self.optimizer.state_dict(),
            'scaler'    : self.scaler.state_dict(),
            # loop_name   : loop_step,
            'epoch': self._cur_epoch,
            'iter':  self._cur_iter,
            'results'   : results,
        }
        torch.save(checkpoint, log_dir / 'last.pt')
        self._save_if_best(checkpoint)

        if self.cfg.ema:
            # no_ema_loss = results['loss']
            # results = self.eval_model(self.ema.module)
            results = self.ema.module.self_evaluate(self.val_img_dir, log_dir=log_dir, steps=self.cfg.val_steps)
            results_to_log = self.process_log_results(results)
            # log_json_like(results)
            _log_dic.update({'val-metrics/ema-'+k: v for k,v in results_to_log.items()})
            # save last checkpoint of EMA
            checkpoint = {
                'model': self.ema.module.state_dict(),
                'epoch': self._cur_epoch,
                'iter':  self._cur_iter,
                'results' : results,
            }
            torch.save(checkpoint, log_dir / 'last_ema.pt')
            self._save_if_best(checkpoint)

        # wandb log
        self.wbrun.log(_log_dic, step=self._cur_iter)
        # Log evaluation results to file
        msg = self.stats_table.get_body() + '||' + '%10.4g' * 1 % (results['loss'])
        with open(log_dir / 'results.txt', 'a') as f:
            f.write(msg + '\n')

        self._results = results
        print()

    def process_log_results(self, results):
        bdr = get_bd_rate_over_anchor(results, self.cfg.valset)
        lambdas = results['lambda']
        results_to_log = {'bd-rate': bdr}
        for idx in [0, len(lambdas)//2, -1]:
            lmb = round(lambdas[idx])
            results_to_log.update({
                f'lmb{lmb}/loss': results['loss'][idx],
                f'lmb{lmb}/bpp':  results['bpp'][idx],
                f'lmb{lmb}/psnr': results['psnr'][idx],
            })
        results['loss'] = bdr
        results['bd-rate'] = bdr
        log_json_like(results)
        # draw R-D curve
        data = [[b,p] for b,p in zip(results['bpp'], results['psnr'])]
        table = wandb.Table(data=data, columns=['bpp', 'psnr'])
        self.wbrun.log(
            {'psnr-rate' : wandb.plot.line(table, 'bpp', 'psnr', title='PSNR-Rate plot')},
            step=self._cur_iter
        )
        return results_to_log

def log_json_like(dict_of_list):
    for k, value in dict_of_list.items():
        if isinstance(value, list):
            vlist_str = '[' + ', '.join([f'{v:.12f}'[:6] for v in value]) + ']'
        else:
            vlist_str = value
        mylog.info(f"'{k:<6s}': {vlist_str}")


def main():
    cfg = parse_args()
    TrainWrapper().main(cfg)


if __name__ == '__main__':
    main()