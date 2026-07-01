"""On-robot Diffusion Policy — self-contained sibling of ACT.py.

Loads the ACT-shaped artifacts the cloud DP preset produces (a
``dp_policy_step_*.pth`` EMA state_dict + ``dataset_stats.pt``) and exposes the
same ``select_action(batch) -> (B, action_dim)`` surface ManipulationServer uses
for ACT, so the inference loop is unchanged.

Self-contained on purpose (like ACT.py): vendors the U-Net, the multi-image obs
encoder, and mean/std normalization with the *exact* module/buffer names used in
training (so ``load_state_dict`` matches), plus a dependency-free DDIM sampler
(no ``diffusers``) matching the training scheduler (cosine betas, epsilon
prediction). The denoising U-Net call is isolated in ``_unet_forward`` so it can
be swapped for a TensorRT engine without touching the sampling loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from collections import deque
from enum import Enum
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
import torchvision


# ── normalization (mirrors training dp/normalize.py exactly) ─────────
class NormalizationMode(str, Enum):
    IDENTITY = "identity"
    MEAN_STD = "mean_std"


class FeatureType(str, Enum):
    VISUAL = "VISUAL"
    STATE = "STATE"
    ACTION = "ACTION"


@dataclass
class PolicyFeature:
    type: FeatureType
    shape: List[int]


def _buffer_shape(feature: PolicyFeature) -> List[int]:
    if feature.type == FeatureType.VISUAL:
        return [feature.shape[0], 1, 1]
    return list(feature.shape)


def _sanitize(key: str) -> str:
    return key.replace(".", "_")


class _NormBase(nn.Module):
    def __init__(self, features, norm_map, stats=None):
        super().__init__()
        self._modes = {}
        for key, feature in features.items():
            mode = norm_map.get(feature.type.value, NormalizationMode.IDENTITY.value)
            self._modes[key] = mode
            if mode != NormalizationMode.MEAN_STD.value:
                continue
            shape = _buffer_shape(feature)
            sk = _sanitize(key)
            mean = torch.full(shape, float("inf"))
            std = torch.full(shape, float("inf"))
            if stats is not None and key in stats:
                if "mean" in stats[key]:
                    mean = stats[key]["mean"].clone().to(torch.float32).view(shape)
                if "std" in stats[key]:
                    std = stats[key]["std"].clone().to(torch.float32).view(shape)
            self.register_buffer(f"{sk}__mean", mean)
            self.register_buffer(f"{sk}__std", std)

    def _mean_std(self, key):
        sk = _sanitize(key)
        return getattr(self, f"{sk}__mean"), getattr(self, f"{sk}__std")


class Normalize(_NormBase):
    def forward(self, batch):
        out = dict(batch)
        for key, mode in self._modes.items():
            if key not in out or mode != NormalizationMode.MEAN_STD.value:
                continue
            mean, std = self._mean_std(key)
            out[key] = (out[key] - mean) / (std + 1e-8)
        return out


class Unnormalize(_NormBase):
    def forward(self, batch):
        out = dict(batch)
        for key, mode in self._modes.items():
            if key not in out or mode != NormalizationMode.MEAN_STD.value:
                continue
            mean, std = self._mean_std(key)
            out[key] = out[key] * std + mean
        return out


DEFAULT_NORM_MAP = {
    FeatureType.VISUAL.value: NormalizationMode.MEAN_STD.value,
    FeatureType.STATE.value: NormalizationMode.MEAN_STD.value,
    FeatureType.ACTION.value: NormalizationMode.MEAN_STD.value,
}


# ── conditional U-Net (mirrors training dp/conditional_unet1d.py) ────
class SinusoidalPosEmb(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, x):
        device = x.device
        half = self.dim // 2
        emb = math.log(10000) / (half - 1)
        emb = torch.exp(torch.arange(half, device=device) * -emb)
        emb = x[:, None] * emb[None, :]
        return torch.cat((emb.sin(), emb.cos()), dim=-1)


class Downsample1d(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv = nn.Conv1d(dim, dim, 3, 2, 1)

    def forward(self, x):
        return self.conv(x)


class Upsample1d(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv = nn.ConvTranspose1d(dim, dim, 4, 2, 1)

    def forward(self, x):
        return self.conv(x)


class Conv1dBlock(nn.Module):
    def __init__(self, inp, out, kernel_size, n_groups=8):
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv1d(inp, out, kernel_size, padding=kernel_size // 2),
            nn.GroupNorm(n_groups, out),
            nn.Mish(),
        )

    def forward(self, x):
        return self.block(x)


class ConditionalResidualBlock1D(nn.Module):
    def __init__(self, in_ch, out_ch, cond_dim, kernel_size=3, n_groups=8, cond_predict_scale=False):
        super().__init__()
        self.blocks = nn.ModuleList(
            [
                Conv1dBlock(in_ch, out_ch, kernel_size, n_groups=n_groups),
                Conv1dBlock(out_ch, out_ch, kernel_size, n_groups=n_groups),
            ]
        )
        cond_channels = out_ch * 2 if cond_predict_scale else out_ch
        self.cond_predict_scale = cond_predict_scale
        self.out_channels = out_ch
        self.cond_encoder = nn.Sequential(
            nn.Mish(), nn.Linear(cond_dim, cond_channels), nn.Unflatten(-1, (-1, 1))
        )
        self.residual_conv = nn.Conv1d(in_ch, out_ch, 1) if in_ch != out_ch else nn.Identity()

    def forward(self, x, cond):
        out = self.blocks[0](x)
        embed = self.cond_encoder(cond)
        if self.cond_predict_scale:
            embed = embed.reshape(embed.shape[0], 2, self.out_channels, 1)
            out = embed[:, 0, ...] * out + embed[:, 1, ...]
        else:
            out = out + embed
        out = self.blocks[1](out)
        return out + self.residual_conv(x)


class ConditionalUnet1D(nn.Module):
    def __init__(self, input_dim, global_cond_dim=None, diffusion_step_embed_dim=128,
                 down_dims=(256, 512, 1024), kernel_size=5, n_groups=8, cond_predict_scale=True):
        super().__init__()
        all_dims = [input_dim] + list(down_dims)
        start_dim = down_dims[0]
        dsed = diffusion_step_embed_dim
        self.diffusion_step_encoder = nn.Sequential(
            SinusoidalPosEmb(dsed), nn.Linear(dsed, dsed * 4), nn.Mish(), nn.Linear(dsed * 4, dsed)
        )
        cond_dim = dsed + (global_cond_dim or 0)
        in_out = list(zip(all_dims[:-1], all_dims[1:]))
        mid_dim = all_dims[-1]

        def blk(a, b):
            return ConditionalResidualBlock1D(a, b, cond_dim, kernel_size, n_groups, cond_predict_scale)

        self.mid_modules = nn.ModuleList([blk(mid_dim, mid_dim), blk(mid_dim, mid_dim)])
        self.down_modules = nn.ModuleList([])
        for ind, (di, do) in enumerate(in_out):
            is_last = ind >= (len(in_out) - 1)
            self.down_modules.append(
                nn.ModuleList([blk(di, do), blk(do, do), Downsample1d(do) if not is_last else nn.Identity()])
            )
        self.up_modules = nn.ModuleList([])
        for ind, (di, do) in enumerate(reversed(in_out[1:])):
            is_last = ind >= (len(in_out) - 1)
            self.up_modules.append(
                nn.ModuleList([blk(do * 2, di), blk(di, di), Upsample1d(di) if not is_last else nn.Identity()])
            )
        self.final_conv = nn.Sequential(
            Conv1dBlock(start_dim, start_dim, kernel_size=kernel_size), nn.Conv1d(start_dim, input_dim, 1)
        )

    def forward(self, sample, timestep, global_cond=None):
        sample = sample.transpose(1, 2)  # (B,T,D) -> (B,D,T)
        if not torch.is_tensor(timestep):
            timestep = torch.tensor([timestep], dtype=torch.long, device=sample.device)
        elif timestep.ndim == 0:
            timestep = timestep[None].to(sample.device)
        timesteps = timestep.expand(sample.shape[0])
        gfeat = self.diffusion_step_encoder(timesteps)
        if global_cond is not None:
            gfeat = torch.cat([gfeat, global_cond], dim=-1)
        x = sample
        h = []
        for resnet, resnet2, downsample in self.down_modules:
            x = resnet(x, gfeat)
            x = resnet2(x, gfeat)
            h.append(x)
            x = downsample(x)
        for m in self.mid_modules:
            x = m(x, gfeat)
        for resnet, resnet2, upsample in self.up_modules:
            skip = h.pop()
            if x.shape[-1] != skip.shape[-1]:
                x = x[..., : skip.shape[-1]]
            x = torch.cat((x, skip), dim=1)
            x = resnet(x, gfeat)
            x = resnet2(x, gfeat)
            x = upsample(x)
        x = self.final_conv(x)
        return x.transpose(1, 2)  # (B,D,T) -> (B,T,D)


# ── obs encoder (mirrors training dp/obs_encoder.py) ─────────────────
def _replace_bn_with_gn(root, fpg=16):
    for name, child in root.named_children():
        if isinstance(child, nn.BatchNorm2d):
            setattr(root, name, nn.GroupNorm(max(1, child.num_features // fpg), child.num_features))
        else:
            _replace_bn_with_gn(child, fpg)
    return root


class MultiImageObsEncoder(nn.Module):
    def __init__(self, rgb_keys, low_dim_keys, obs_shapes, resnet_name="resnet18", use_group_norm=True):
        super().__init__()
        self.rgb_keys = list(rgb_keys)
        self.low_dim_keys = list(low_dim_keys)
        self.obs_shapes = obs_shapes
        self.key_model_map = nn.ModuleDict()
        for key in self.rgb_keys:
            model = getattr(torchvision.models, resnet_name)(weights=None)
            model.fc = nn.Identity()
            if use_group_norm:
                model = _replace_bn_with_gn(model)
            self.key_model_map[_sanitize(key)] = model
        self._output_dim = self._compute_output_dim()

    @torch.no_grad()
    def _compute_output_dim(self):
        ex = {}
        for key in self.rgb_keys:
            c, h, w = self.obs_shapes[key]
            ex[key] = torch.zeros(1, c, h, w)
        for key in self.low_dim_keys:
            ex[key] = torch.zeros(1, *self.obs_shapes[key])
        return self.forward(ex).shape[-1]

    @property
    def output_dim(self):
        return self._output_dim

    def forward(self, obs_dict):
        feats = []
        for key in self.rgb_keys:
            feats.append(self.key_model_map[_sanitize(key)](obs_dict[key]))
        for key in self.low_dim_keys:
            feats.append(obs_dict[key])
        return torch.cat(feats, dim=-1)


# ── DDIM sampler (no diffusers; matches DDPM cosine betas) ───────────
def _cosine_alphas_cumprod(num_train_timesteps: int) -> torch.Tensor:
    """``squaredcos_cap_v2`` schedule (diffusers betas_for_alpha_bar)."""
    def alpha_bar(t):
        return math.cos((t + 0.008) / 1.008 * math.pi / 2) ** 2

    betas = []
    for i in range(num_train_timesteps):
        t1 = i / num_train_timesteps
        t2 = (i + 1) / num_train_timesteps
        betas.append(min(1 - alpha_bar(t2) / alpha_bar(t1), 0.999))
    alphas = 1.0 - torch.tensor(betas, dtype=torch.float64)
    return torch.cumprod(alphas, dim=0)


class DDIMSampler:
    """Deterministic DDIM (eta=0), epsilon prediction, clip_sample=True."""

    def __init__(self, num_train_timesteps=100, num_inference_steps=16, clip_sample=True):
        self.acp = _cosine_alphas_cumprod(num_train_timesteps)
        self.final_alpha = torch.tensor(1.0, dtype=torch.float64)  # set_alpha_to_one
        self.step_ratio = num_train_timesteps // num_inference_steps
        self.timesteps = [int(t) for t in (
            torch.arange(0, num_inference_steps) * self.step_ratio
        ).round().flip(0).tolist()]
        self.clip_sample = clip_sample

    def step(self, eps, t, sample):
        prev_t = t - self.step_ratio
        a_t = self.acp[t]
        a_prev = self.acp[prev_t] if prev_t >= 0 else self.final_alpha
        a_t = a_t.to(sample.device, sample.dtype)
        a_prev = a_prev.to(sample.device, sample.dtype)
        beta_t = 1 - a_t
        x0 = (sample - beta_t.sqrt() * eps) / a_t.sqrt()
        if self.clip_sample:
            x0 = x0.clamp(-1, 1)
        direction = (1 - a_prev).sqrt() * eps
        return a_prev.sqrt() * x0 + direction


# ── policy ───────────────────────────────────────────────────────────
@dataclass
class DPRuntimeConfig:
    input_shapes: Dict[str, List[int]] = field(default_factory=dict)
    output_shapes: Dict[str, List[int]] = field(default_factory=dict)
    n_obs_steps: int = 1
    horizon: int = 16
    n_action_steps: int = 8
    num_train_timesteps: int = 100
    num_inference_steps: int = 16
    down_dims: Tuple[int, ...] = (256, 512, 1024)
    kernel_size: int = 5
    n_groups: int = 8
    diffusion_step_embed_dim: int = 128
    cond_predict_scale: bool = True
    vision_backbone: str = "resnet18"
    use_group_norm: bool = True

    def _features(self, shapes):
        out = {}
        for k, s in shapes.items():
            t = FeatureType.VISUAL if "image" in k else (FeatureType.ACTION if k == "action" else FeatureType.STATE)
            out[k] = PolicyFeature(type=t, shape=list(s))
        return out

    @property
    def input_features(self):
        return self._features(self.input_shapes)

    @property
    def output_features(self):
        return self._features(self.output_shapes)

    @property
    def image_keys(self):
        return [k for k in self.input_shapes if "image" in k]

    @property
    def state_keys(self):
        return [k for k in self.input_shapes if "image" not in k]

    @property
    def action_dim(self):
        return self.output_shapes["action"][0]


class DiffusionPolicy(nn.Module):
    def __init__(self, config: DPRuntimeConfig, dataset_stats=None):
        super().__init__()
        self.config = config
        self.normalize_inputs = Normalize(config.input_features, DEFAULT_NORM_MAP, dataset_stats)
        self.normalize_targets = Normalize(config.output_features, DEFAULT_NORM_MAP, dataset_stats)
        self.unnormalize_outputs = Unnormalize(config.output_features, DEFAULT_NORM_MAP, dataset_stats)
        self.obs_encoder = MultiImageObsEncoder(
            config.image_keys, config.state_keys, config.input_shapes,
            config.vision_backbone, config.use_group_norm,
        )
        self.unet = ConditionalUnet1D(
            input_dim=config.action_dim,
            global_cond_dim=self.obs_encoder.output_dim * config.n_obs_steps,
            diffusion_step_embed_dim=config.diffusion_step_embed_dim,
            down_dims=config.down_dims, kernel_size=config.kernel_size,
            n_groups=config.n_groups, cond_predict_scale=config.cond_predict_scale,
        )
        self.sampler = DDIMSampler(config.num_train_timesteps, config.num_inference_steps)
        self._action_queue: deque = deque([], maxlen=config.n_action_steps)
        # Optional TRT engines (see dp_trt.py); None = eager torch. Attached post-load.
        self._trt_unet = None
        self._trt_encoder = None

    def reset(self):
        self._action_queue = deque([], maxlen=self.config.n_action_steps)

    def _unet_forward(self, sample, t, global_cond):
        """Isolated denoising call — a TRT engine (dp_trt.TRTUNet) when attached, else eager."""
        if self._trt_unet is not None:
            return self._trt_unet(sample, t, global_cond)
        return self.unet(sample, t, global_cond=global_cond)

    def _encode_obs(self, nb):
        cfg = self.config
        B = nb[cfg.image_keys[0]].shape[0]
        per_step = {}
        for key in cfg.image_keys + cfg.state_keys:
            x = nb[key]
            is_image = key in cfg.image_keys
            if (is_image and x.ndim == 4) or (not is_image and x.ndim == 2):
                x = x.unsqueeze(1)
            To = x.shape[1]
            per_step[key] = x[:, :To].reshape(B * To, *x.shape[2:])
        return self.obs_encoder(per_step).reshape(B, -1)

    @torch.no_grad()
    def predict_action(self, batch):
        cfg = self.config
        if self._trt_encoder is not None:
            gcond = self._trt_encoder(batch)
        else:
            gcond = self._encode_obs(self.normalize_inputs(batch))
        B = gcond.shape[0]
        traj = torch.randn(B, cfg.horizon, cfg.action_dim, device=gcond.device)
        for t in self.sampler.timesteps:
            eps = self._unet_forward(traj, t, gcond)
            traj = self.sampler.step(eps, t, traj)
        actions = self.unnormalize_outputs({"action": traj})["action"]
        start = cfg.n_obs_steps - 1
        return actions[:, start:start + cfg.n_action_steps]

    @torch.no_grad()
    def select_action(self, batch):
        self.eval()
        if len(self._action_queue) == 0:
            actions = self.predict_action(batch)
            self._action_queue.extend(actions.transpose(0, 1))
        return self._action_queue.popleft()


def load_dp_policy(checkpoint_path, dataset_stats, action_dim, device,
                   n_action_steps=None, **overrides):
    """Build a DiffusionPolicy, load EMA weights + stats, return it ready to run.

    Mirrors ACTPolicy loading in manipulation_server: tolerant to compile/DDP
    prefixes and ``strict=False`` key matching.
    """
    cfg = DPRuntimeConfig(
        input_shapes={
            "observation.image_camera_1": [3, 224, 224],
            "observation.image_camera_2": [3, 224, 224],
            "observation.state": [6],
        },
        output_shapes={"action": [action_dim]},
        **overrides,
    )
    if n_action_steps:
        cfg.n_action_steps = n_action_steps

    state_dict = torch.load(checkpoint_path, map_location=device)
    if isinstance(state_dict, dict) and "state_dict" in state_dict:
        state_dict = state_dict["state_dict"]
    cleaned = {}
    for k, v in state_dict.items():
        if k.startswith("module."):
            k = k[len("module."):]
        if k.startswith("_orig_mod."):
            k = k[len("_orig_mod."):]
        cleaned[k] = v

    policy = DiffusionPolicy(cfg, dataset_stats=dataset_stats).to(device)
    result = policy.load_state_dict(cleaned, strict=False)
    policy.eval()
    policy.reset()
    return policy, result
