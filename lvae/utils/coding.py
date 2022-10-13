import sys
import json
import math
import pickle
import numpy as np
import torchvision.transforms.functional as tvf


def get_object_size(obj, unit='bits'):
    assert unit == 'bits'
    return sys.getsizeof(pickle.dumps(obj)) * 8


def pad_divisible_by(img, div=64):
    """ Pad a PIL.Image such that both its sides are divisible by `div`

    Args:
        img (PIL.Image): input image
        div (int, optional): denominator. Defaults to 64.

    Returns:
        PIL.Image: padded image
    """
    h_old, w_old = img.height, img.width
    if (h_old % div == 0) and (w_old % div == 0):
        return img
    h_tgt = round(div * math.ceil(h_old / div))
    w_tgt = round(div * math.ceil(w_old / div))
    # left, top, right, bottom
    padding = (0, 0, (w_tgt - w_old), (h_tgt - h_old))
    padded = tvf.pad(img, padding=padding, padding_mode='edge')
    return padded


def crop_divisible_by(img, div=64):
    ''' Center crop a PIL.Image such that both its sides are divisible by `div`

    Args:
        img (PIL.Image): input image
        div (int, optional): denominator. Defaults to 64.

    Returns:
        PIL.Image: cropped image
    '''
    h_old, w_old = img.height, img.width
    if (h_old % div == 0) and (w_old % div == 0):
        return img
    h_new = div * (h_old // div)
    w_new = div * (w_old // div)
    cropped = tvf.center_crop(img, output_size=(h_new, w_new))
    return cropped


def bd_rate(r1, psnr1, r2, psnr2):
    """ Compute average bit rate saving of RD-2 over RD-1.

    Equivalent to the implementations in:
    https://github.com/Anserw/Bjontegaard_metric/blob/master/bjontegaard_metric.py
    https://github.com/google/compare-codecs/blob/master/lib/visual_metrics.py

    args:
        r1    (list, np.ndarray): baseline rate
        psnr1 (list, np.ndarray): baseline psnr
        r2    (list, np.ndarray): rate 2
        psnr2 (list, np.ndarray): psnr 2
    """
    lr1 = np.log(r1)
    lr2 = np.log(r2)

    # fit each curve by a polynomial
    degree = 3
    p1 = np.polyfit(psnr1, lr1, deg=degree)
    p2 = np.polyfit(psnr2, lr2, deg=degree)
    # compute integral of the polynomial
    p_int1 = np.polyint(p1)
    p_int2 = np.polyint(p2)
    # area under the curve = integral(max) - integral(min)
    min_psnr = max(min(psnr1), min(psnr2))
    max_psnr = min(max(psnr1), max(psnr2))
    auc1 = np.polyval(p_int1, max_psnr) - np.polyval(p_int1, min_psnr)
    auc2 = np.polyval(p_int2, max_psnr) - np.polyval(p_int2, min_psnr)

    # find avgerage difference
    avg_exp_diff = (auc2 - auc1) / (max_psnr - min_psnr)
    avg_diff = (np.exp(avg_exp_diff) - 1) * 100

    if False: # debug
        import matplotlib.pyplot as plt
        plt.figure(figsize=(8,6))
        l1 = plt.plot(psnr1, lr1, label='PSNR-logBPP 1',
                      marker='.', markersize=12, linestyle='None')
        l2 = plt.plot(psnr2, lr2, label='PSNR-logBPP 2',
                      marker='.', markersize=12, linestyle='None')
        x = np.linspace(min_psnr, max_psnr, num=100)
        # x = np.linspace(min_psnr-10, max_psnr+10, num=100)
        plt.plot(x, np.polyval(p1, x), label='polyfit 1',
                 linestyle='-', color=l1[0].get_color())
        plt.plot(x, np.polyval(p2, x), label='polyfit 2',
                 linestyle='-', color=l2[0].get_color())
        plt.legend(loc='lower right')
        plt.xlim(np.concatenate([psnr1,psnr2]).min()-1, np.concatenate([psnr1,psnr2]).max()+1)
        plt.ylim(np.concatenate([lr1, lr2]).min()-0.1, np.concatenate([lr1, lr2]).max()+0.1)
        # plt.ylim(np.concatenate(lr1, lr2).min(), max(np.concatenate(lr1, lr2)))
        plt.show()
    return avg_diff


class RDList():
    def __init__(self) -> None:
        self.stats_all = []
        self.bdrate_anchor = None

    def add_json(self, fpath, label='no label', **kwargs):
        with open(fpath, mode='r') as f:
            stat = json.load(f)
        if 'results' in stat:
            stat = stat['results']
        stat['label'] = label
        stat['kwargs'] = kwargs
        self.stats_all.append(stat)

    def set_video_info(self, fps, height, width):
        # self.video_info = (fps, height, width)
        self.norm_factor = float(fps * height * width)

    def add_json_bpms(self, fpath, label='no label', **kwargs):
        with open(fpath, mode='r') as f:
            stat = json.load(f)
        if 'results' in stat:
            stat = stat['results']
        # convert bits per second (bps) to bits per pixel (bpp)
        # fps, fh, fw = self.video_info
        # stat['bpp'] = stat['bps'] / (fps * fh * fw)
        stat['bpp'] = [r * 1000 / self.norm_factor for r in stat['bitrate']]
        stat['psnr'] = stat['psnr-rgb']
        stat['label'] = label
        stat['kwargs'] = kwargs
        self.stats_all.append(stat)

    def add_data(self, bpp=[], psnr=[], label='no label', **kwargs):
        stat = {
            'bpp': bpp,
            'psnr': psnr,
            'label': label
        }
        stat['kwargs'] = kwargs
        self.stats_all.append(stat)

    def set_bdrate_anchor(self, label=None):
        if label is None:
            anchor = self.stats_all[-1]
        else:
            anchor = [st for st in self.stats_all if (st['label'] == label)]
            assert len(anchor) == 1
            anchor = anchor[0]
        self.bdrate_anchor = anchor

    def compute_bdrate(self):
        if self.bdrate_anchor is None:
            return
        bd_anchor = self.bdrate_anchor
        print(f'BD-rate anchor = {bd_anchor["label"]}')
        for method in self.stats_all:
            if len(method['bpp']) == 0:
                continue
            bd = bd_rate(bd_anchor['bpp'], bd_anchor['psnr'],
                         method['bpp'], method['psnr'])
            print(method['label'], f'BD-rate = {bd}')
        print()

    def plot_all_stats(self, ax):
        for stat in self.stats_all:
            self._plot_stat(stat, ax=ax, **stat['kwargs'])

    @staticmethod
    def _plot_stat(stat, ax, ls='-', **kwargs):
        assert 'bpp' in stat, f'{stat}'
        x = stat['bpp']
        y = stat['psnr']
        label = stat['label']
        kwargs['marker'] = kwargs.get('marker', '.')
        kwargs['linewidth'] = kwargs.get('linewidth', 1.2)
        p = ax.plot(x, y, label=label, markersize=8, linestyle=ls, **kwargs)
        return p
