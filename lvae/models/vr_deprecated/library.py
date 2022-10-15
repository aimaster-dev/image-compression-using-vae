import math
from torch.hub import load_state_dict_from_url

from lvae.models.registry import register_model
import lvae.models.common as common
import lvae.models.vr_deprecated.model as vrm


@register_model
def vr_small(lmb_range=[16,1024], lmb_embed_dim=(256, 256), sin_period=64, pretrained=False):
    """ variable rate model

    Args:
        lmb_range (list): lambda range. Defaults to [16,1024].
        lmb_embed_dim (tuple): lmb embedding dimension. Defaults to (256, 256).
        sin_period (int): max peiod used in sin embedding. Defaults to 64.
    """
    cfg = dict()

    # variable rate
    cfg['log_lmb_range'] = (math.log(lmb_range[0]), math.log(lmb_range[1]))
    cfg['lmb_embed_dim'] = lmb_embed_dim
    cfg['sin_period'] = sin_period
    _emb_dim = cfg['lmb_embed_dim'][1]

    ch = 96 # 128
    enc_nums = [4, 4, 4, 2, 2]
    dec_nums = [1, 1, 2, 2, 2]
    enc_dims = [144, ch*3, ch*4, ch*4, ch*4]
    dec_dims = [ch*4, ch*4, ch*3, ch*2, ch*1]
    z_dims = [32, 32, 16, 16, 16]

    im_channels = 3
    cfg['enc_blocks'] = [
        common.patch_downsample(im_channels, enc_dims[0], rate=4),
        *[vrm.MyConvNeXtBlockAdaLN(enc_dims[0], _emb_dim, kernel_size=7) for _ in range(enc_nums[0])], # 16x16
        vrm.MyConvNeXtAdaLNPatchDown(enc_dims[0], enc_dims[1], embed_dim=_emb_dim, kernel_size=7),
        *[vrm.MyConvNeXtBlockAdaLN(enc_dims[1], _emb_dim, kernel_size=7) for _ in range(enc_nums[1])], # 8x8
        vrm.MyConvNeXtAdaLNPatchDown(enc_dims[1], enc_dims[2], embed_dim=_emb_dim, kernel_size=7),
        *[vrm.MyConvNeXtBlockAdaLN(enc_dims[2], _emb_dim, kernel_size=5) for _ in range(enc_nums[2])], # 4x4
        vrm.MyConvNeXtAdaLNPatchDown(enc_dims[2], enc_dims[3], embed_dim=_emb_dim, kernel_size=7),
        *[vrm.MyConvNeXtBlockAdaLN(enc_dims[3], _emb_dim, kernel_size=3) for _ in range(enc_nums[3])], # 2x2
        vrm.MyConvNeXtAdaLNPatchDown(enc_dims[3], enc_dims[3], embed_dim=_emb_dim, kernel_size=7),
        *[vrm.MyConvNeXtBlockAdaLN(enc_dims[3], _emb_dim, kernel_size=1) for _ in range(enc_nums[4])], # 1x1
    ]
    cfg['dec_blocks'] = [
        *[vrm.VRLatentBlock3Pos(dec_dims[0], z_dims[0], _emb_dim, enc_width=enc_dims[-1], kernel_size=1, mlp_ratio=4) for _ in range(dec_nums[0])], # 1x1
        common.patch_upsample(dec_dims[0], dec_dims[1], rate=2),
        *[vrm.VRLatentBlock3Pos(dec_dims[1], z_dims[1], _emb_dim, enc_width=enc_dims[-2], kernel_size=3, mlp_ratio=3) for _ in range(dec_nums[1])], # 2x2
        common.patch_upsample(dec_dims[1], dec_dims[2], rate=2),
        *[vrm.VRLatentBlock3Pos(dec_dims[2], z_dims[2], _emb_dim, enc_width=enc_dims[-3], kernel_size=5, mlp_ratio=2) for _ in range(dec_nums[2])], # 4x4
        common.patch_upsample(dec_dims[2], dec_dims[3], rate=2),
        *[vrm.VRLatentBlock3Pos(dec_dims[3], z_dims[3], _emb_dim, enc_width=enc_dims[-4], kernel_size=7, mlp_ratio=1.75) for _ in range(dec_nums[3])], # 8x8
        common.patch_upsample(dec_dims[3], dec_dims[4], rate=2),
        *[vrm.VRLatentBlock3Pos(dec_dims[4], z_dims[4], _emb_dim, enc_width=enc_dims[-5], kernel_size=7, mlp_ratio=1.5) for _ in range(dec_nums[4])], # 16x16
        common.patch_upsample(dec_dims[4], im_channels, rate=4)
    ]
    cfg['out_net'] = vrm.VarLambdaMSEOutputNet()

    # mean and std computed on imagenet
    cfg['im_shift'] = -0.4546259594901961
    cfg['im_scale'] = 3.67572653978347
    cfg['max_stride'] = 64

    cfg['log_images'] = ['collie64.png', 'gun128.png', 'motor256.png']

    model = vrm.VariableRateLossyVAE(cfg)
    if pretrained:
        url = 'https://huggingface.co/duanzh0/my-model-weights/resolve/main/tpc_lm3pz_enc-coco-last.pt'
        msd = load_state_dict_from_url(url)['model']
        model.load_state_dict(msd, strict=False)
    return model
