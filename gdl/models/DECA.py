import os, sys
import torch
import torchvision
import torch.nn.functional as F
import adabound
from pytorch_lightning import LightningModule
from pytorch_lightning.loggers import WandbLogger
from ..layers.losses.EmoNetLoss import EmoNetLoss, create_emo_loss
import numpy as np
# from time import time
from skimage.io import imread
import cv2
from pathlib import Path

from .Renderer import SRenderY
from .DecaEncoder import ResnetEncoder, SecondHeadResnet, SwinEncoder
from .DecaDecoder import Generator
from .DecaFLAME import FLAME, FLAMETex
# from .MLP import MLP
from .EmotionMLP import EmotionMLP

import gdl.layers.losses.DecaLosses as lossfunc
import gdl.utils.DecaUtils as util
from gdl.datasets.FaceVideoDataset import Expression7
from gdl.datasets.AffectNetDataModule import AffectNetExpressions
from gdl.utils.lightning_logging import _log_array_image, _log_wandb_image, _torch_image2np

torch.backends.cudnn.benchmark = True
from enum import Enum
from gdl.utils.other import class_from_str
from gdl.layers.losses.VGGLoss import VGG19Loss

class DecaMode(Enum):
    COARSE = 1
    DETAIL = 2


class DecaModule(LightningModule):

    def __init__(self, model_params, learning_params, inout_params, stage_name = ""):
        super().__init__()
        self.learning_params = learning_params
        self.inout_params = inout_params
        if 'deca_class' not in model_params.keys() or model_params.deca_class is None:
            print(f"Deca class is not specified. Defaulting to {str(DECA.__class__.__name__)}")
            deca_class = DECA
        else:
            deca_class = class_from_str(model_params.deca_class, sys.modules[__name__])

        self.deca = deca_class(config=model_params)
        self.mode = DecaMode[str(model_params.mode).upper()]
        self.stage_name = stage_name
        if self.stage_name is None:
            self.stage_name = ""
        if len(self.stage_name) > 0:
            self.stage_name += "_"
        self.emonet_loss = None
        self._init_emotion_loss()

        if 'mlp_emotion_predictor' in self.deca.config.keys():
            # self._build_emotion_mlp(self.deca.config.mlp_emotion_predictor)
            self.emotion_mlp = EmotionMLP(self.deca.config.mlp_emotion_predictor, model_params)
        else:
            self.emotion_mlp = None

    def _init_emotion_loss(self):
        if 'emonet_weight' in self.deca.config.keys():
            if self.emonet_loss is not None:
                emoloss_force_override = True if 'emoloss_force_override' in self.deca.config.keys() and self.deca.config.emoloss_force_override else False
                if self.emonet_loss.is_trainable():
                    if not emoloss_force_override:
                        print("The old emonet loss is trainable and will not be overrided or replaced.")
                        return
                        # raise NotImplementedError("The old emonet loss was trainable. Changing a trainable loss is probably now "
                        #                       "what you want implicitly. If you need this, use the '`'emoloss_force_override' config.")
                    else:
                        print("The old emonet loss is trainable but override is set so it will be replaced.")

            if 'emonet_model_path' in self.deca.config.keys():
                emonet_model_path = self.deca.config.emonet_model_path
            else:
                emonet_model_path=None
            # self.emonet_loss = EmoNetLoss(self.device, emonet=emonet_model_path)
            emoloss_trainable = True if 'emoloss_trainable' in self.deca.config.keys() and self.deca.config.emoloss_trainable else False
            emoloss_dual = True if 'emoloss_dual' in self.deca.config.keys() and self.deca.config.emoloss_dual else False
            old_emonet_loss = self.emonet_loss

            self.emonet_loss = create_emo_loss(self.device, emoloss=emonet_model_path, trainable=emoloss_trainable, dual=emoloss_dual)

            if old_emonet_loss is not None and type(old_emonet_loss) != self.emonet_loss:
                print(f"The old emonet loss {old_emonet_loss.__class__.__name__} will be replaced during reconfiguration by "
                      f"new emotion loss {self.emonet_loss.__class__.__name__}")

        else:
            self.emonet_loss = None

    def reconfigure(self, model_params, inout_params, stage_name="", downgrade_ok=False, train=True):
        if (self.mode == DecaMode.DETAIL and model_params.mode != DecaMode.DETAIL) and not downgrade_ok:
            raise RuntimeError("You're switching the DECA mode from DETAIL to COARSE. Is this really what you want?!")
        self.inout_params = inout_params

        if self.deca.__class__.__name__ != model_params.deca_class:
            old_deca_class = self.deca.__class__.__name__
            state_dict = self.deca.state_dict()
            deca_class = class_from_str(model_params.deca_class, sys.modules[__name__])
            self.deca = deca_class(config=model_params)

            diff = set(state_dict.keys()).difference(set(self.deca.state_dict().keys()))
            if len(diff) > 0:
                raise RuntimeError(f"Some values from old state dict will not be used. This is probably not what you "
                                   f"want because it most likely means that the pretrained model's weights won't be used. "
                                   f"Maybe you messed up backbone compatibility (i.e. SWIN vs ResNet?) {diff}")
            ret = self.deca.load_state_dict(state_dict, strict=False)
            if len(ret.unexpected_keys) > 0:
                raise print(f"Unexpected keys: {ret.unexpected_keys}")
            missing_modules = set([s.split(".")[0] for s in ret.missing_keys])
            print(f"Missing modules when upgrading from {old_deca_class} to {model_params.deca_class}:")
            print(missing_modules)
        else:
            self.deca._reconfigure(model_params)

        self._init_emotion_loss()

        self.stage_name = stage_name
        if self.stage_name is None:
            self.stage_name = ""
        if len(self.stage_name) > 0:
            self.stage_name += "_"
        self.mode = DecaMode[str(model_params.mode).upper()]
        self.train(mode=train)
        print(f"DECA MODE RECONFIGURED TO: {self.mode}")

        if 'shape_contrain_type' in self.deca.config.keys() and str(self.deca.config.shape_constrain_type).lower() != 'none':
            shape_constraint = self.deca.config.shape_constrain_type
        else:
            shape_constraint = None
        if 'expression_constrain_type' in self.deca.config.keys() and str(self.deca.config.expression_constrain_type).lower() != 'none':
            expression_constraint = self.deca.config.expression_constrain_type
        else:
            expression_constraint = None

        if shape_constraint is not None and expression_constraint is not None:
            raise ValueError("Both shape constraint and expression constraint are active. This is probably not what we want.")

    def train(self, mode: bool = True):
        # super().train(mode) # not necessary
        self.deca.train(mode)

        if self.emotion_mlp is not None:
            self.emotion_mlp.train(mode)

        if self.emonet_loss is not None:
            self.emonet_loss.eval()

        if self.deca.perceptual_loss is not None:
            self.deca.perceptual_loss.eval()
        if self.deca.id_loss is not None:
            self.deca.id_loss.eval()

        return self

    def to(self, *args, **kwargs):
        super().to(*args, **kwargs)
        return self

    def cuda(self, device=None):
        super().cuda(device)
        return self

    def cpu(self):
        super().cpu()
        return self

    def forward(self, batch):
        values = self.encode(batch, training=False)
        values = self.decode(values, training=False)
        return values

    def _encode_flame(self, images):
        if self.mode == DecaMode.COARSE or \
                (self.mode == DecaMode.DETAIL and self.deca.config.train_coarse):
            parameters = self.deca._encode_flame(images)
        elif self.mode == DecaMode.DETAIL:
            with torch.no_grad():
                parameters = self.deca._encode_flame(images)
        else:
            raise ValueError(f"Invalid DECA Mode {self.mode}")
        code_list = self.deca.decompose_code(parameters)
        shapecode, texcode, expcode, posecode, cam, lightcode = code_list
        return shapecode, texcode, expcode, posecode, cam, lightcode

    def _expression_ring_exchange(self, original_batch_size, K,
                                  expcode, posecode, shapecode, lightcode, texcode,
                                  images, cam, lmk, masks, va, expr7, affectnetexp, detailcode=None, exprw=None):
        new_order = np.array([np.random.permutation(K) + i * K for i in range(original_batch_size)])
        new_order = new_order.flatten()
        expcode_new = expcode[new_order]
        ## append new shape code data
        expcode = torch.cat([expcode, expcode_new], dim=0)
        texcode = torch.cat([texcode, texcode], dim=0)
        shapecode = torch.cat([shapecode, shapecode], dim=0)

        globpose = posecode[..., :3]
        jawpose = posecode[..., 3:]

        if self.deca.config.expression_constrain_use_jaw_pose:
            jawpose_new = jawpose[new_order]
            jawpose = torch.cat([jawpose, jawpose_new], dim=0)
        else:
            jawpose = torch.cat([jawpose, jawpose], dim=0)

        if self.deca.config.expression_constrain_use_global_pose:
            globpose_new = globpose[new_order]
            globpose = torch.cat([globpose, globpose_new], dim=0)
        else:
            globpose = torch.cat([globpose, globpose], dim=0)

        if self.deca.config.expression_constrain_use_jaw_pose or self.deca.config.expression_constrain_use_global_pose:
            posecode = torch.cat([globpose, jawpose], dim=-1)
            # posecode_new = torch.cat([globpose, jawpose], dim=-1)
        else:
            # posecode_new = posecode
            # posecode_new = posecode
            posecode = torch.cat([posecode, posecode], dim=0)

        cam = torch.cat([cam, cam], dim=0)
        lightcode = torch.cat([lightcode, lightcode], dim=0)
        ## append gt
        images = torch.cat([images, images],
                           dim=0)  # images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])
        lmk = torch.cat([lmk, lmk], dim=0)  # lmk = lmk.view(-1, lmk.shape[-2], lmk.shape[-1])
        masks = torch.cat([masks, masks], dim=0)


        # NOTE:
        # Here we could think about what makes sense to exchange
        # 1) Do we exchange all emotion GT (VA and expression) within the ring?
        # 2) Do we exchange only the GT on which the ring is constructed (AffectNet ring based on binned VA or expression or Emonet feature?)
        # note: if we use EmoMLP that goes from (expression, jawpose, detailcode) -> (v,a,expr) and we exchange
        # ALL of these, the EmoMLP prediction will of course be the same. The output image still changes,
        # so EmoNet loss (if used) would be different. Same for the photometric/landmark losses.

        # TODO:
        # For now I decided to exchange everything but this should probably be experimented with
        # I would argue though, that exchanging the GT is the right thing to do
        if va is not None:
            va = torch.cat([va, va[new_order]], dim=0)
        if expr7 is not None:
            expr7 = torch.cat([expr7, expr7[new_order]], dim=0)
        if affectnetexp is not None:
            affectnetexp = torch.cat([affectnetexp, affectnetexp[new_order]], dim=0)
        if exprw is not None:
            exprw = torch.cat([exprw, exprw[new_order]], dim=0)

        if detailcode is not None:
            #TODO: to exchange or not to exchange, that is the question, the answer is probably yes

            # detailcode = torch.cat([detailcode, detailcode], dim=0)
            detailcode = torch.cat([detailcode, detailcode[new_order]], dim=0)
        return expcode, posecode, shapecode, lightcode, texcode, images, cam, lmk, masks, va, expr7, affectnetexp, detailcode, exprw

        # return expcode, posecode, shapecode, lightcode, texcode, images, cam, lmk, masks, va, expr7


    def encode(self, batch, training=True) -> dict:
        codedict = {}
        original_batch_size = batch['image'].shape[0]

        # [B, K, 3, size, size] ==> [BxK, 3, size, size]
        images = batch['image']

        if len(images.shape) == 5:
            K = images.shape[1]
        elif len(images.shape) == 4:
            K = 1
        else:
            raise RuntimeError("Invalid image batch dimensions.")

        # print("Batch size!")
        # print(images.shape)
        images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])

        if 'landmark' in batch.keys():
            lmk = batch['landmark']
            lmk = lmk.view(-1, lmk.shape[-2], lmk.shape[-1])
        if 'mask' in batch.keys():
            masks = batch['mask']
            masks = masks.view(-1, images.shape[-2], images.shape[-1])

        #TODO: TAKE CARE OF the CASE WHEN va, expr7 and au8 are NaN (the label does not exist)!!!
        if 'va' in batch:
            va = batch['va']
            va = va.view(-1, va.shape[-1])
        else:
            va = None

        if 'expr7' in batch:
            expr7 = batch['expr7']
            expr7 = expr7.view(-1, expr7.shape[-1])
        else:
            expr7 = None

        if 'affectnetexp' in batch:
            affectnetexp = batch['affectnetexp']
            affectnetexp = affectnetexp.view(-1, affectnetexp.shape[-1])
        else:
            affectnetexp = None

        if 'expression_weight' in batch:
            exprw = batch['expression_weight']
            exprw = exprw.view(-1, exprw.shape[-1])
        else:
            exprw = None

        shapecode, texcode, expcode, posecode, cam, lightcode = self._encode_flame(images)

        # #TODO: figure out if we want to keep this code block:
        # if self.config.model.jaw_type == 'euler':
        #     # if use euler angle
        #     euler_jaw_pose = posecode[:, 3:].clone()  # x for yaw (open mouth), y for pitch (left ang right), z for roll
        #     # euler_jaw_pose[:,0] = 0.
        #     # euler_jaw_pose[:,1] = 0.
        #     # euler_jaw_pose[:,2] = 30.
        #     posecode[:, 3:] = batch_euler2axis(euler_jaw_pose)

        if training:
            if self.mode == DecaMode.COARSE:
                ### shape constraints
                if self.deca.config.shape_constrain_type == 'same':
                    # reshape shapecode => [B, K, n_shape]
                    # shapecode_idK = shapecode.view(self.batch_size, self.deca.K, -1)
                    shapecode_idK = shapecode.view(original_batch_size, K, -1)
                    # get mean id
                    shapecode_mean = torch.mean(shapecode_idK, dim=[1])
                    # shapecode_new = shapecode_mean[:, None, :].repeat(1, self.deca.K, 1)
                    shapecode_new = shapecode_mean[:, None, :].repeat(1, K, 1)
                    shapecode = shapecode_new.view(-1, self.deca.config.model.n_shape)
                elif self.deca.config.shape_constrain_type == 'exchange':
                    '''
                    make sure s0, s1 is something to make shape close
                    the difference from ||so - s1|| is 
                    the later encourage s0, s1 is cloase in l2 space, but not really ensure shape will be close
                    '''
                    # new_order = np.array([np.random.permutation(self.deca.config.train_K) + i * self.deca.config.train_K for i in range(self.deca.config.batch_size_train)])
                    # new_order = np.array([np.random.permutation(self.deca.config.train_K) + i * self.deca.config.train_K for i in range(original_batch_size)])
                    new_order = np.array([np.random.permutation(K) + i * K for i in range(original_batch_size)])
                    new_order = new_order.flatten()
                    shapecode_new = shapecode[new_order]
                    ## append new shape code data
                    shapecode = torch.cat([shapecode, shapecode_new], dim=0)
                    texcode = torch.cat([texcode, texcode], dim=0)
                    expcode = torch.cat([expcode, expcode], dim=0)
                    posecode = torch.cat([posecode, posecode], dim=0)
                    cam = torch.cat([cam, cam], dim=0)
                    lightcode = torch.cat([lightcode, lightcode], dim=0)
                    ## append gt
                    images = torch.cat([images, images],
                                       dim=0)  # images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])
                    lmk = torch.cat([lmk, lmk], dim=0)  # lmk = lmk.view(-1, lmk.shape[-2], lmk.shape[-1])
                    masks = torch.cat([masks, masks], dim=0)

                    if va is not None:
                        va = torch.cat([va, va], dim=0)
                    if expr7 is not None:
                        expr7 = torch.cat([expr7, expr7], dim=0)

                elif 'expression_constrain_type' in self.deca.config.keys() and \
                        self.deca.config.expression_constrain_type == 'same':
                    # reshape shapecode => [B, K, n_shape]
                    # shapecode_idK = shapecode.view(self.batch_size, self.deca.K, -1)
                    expcode_idK = expcode.view(original_batch_size, K, -1)
                    # get mean id
                    expcode_mean = torch.mean(expcode_idK, dim=[1])
                    # shapecode_new = shapecode_mean[:, None, :].repeat(1, self.deca.K, 1)
                    expcode_new = expcode_mean[:, None, :].repeat(1, K, 1)
                    expcode = expcode_new.view(-1, self.deca.config.model.n_shape)

                elif 'expression_constrain_type' in self.deca.config.keys() and \
                        self.deca.config.expression_constrain_type == 'exchange':
                    expcode, posecode, shapecode, lightcode, texcode, images, cam, lmk, masks, va, expr7, affectnetexp, _, exprw = \
                        self._expression_ring_exchange(original_batch_size, K,
                                  expcode, posecode, shapecode, lightcode, texcode,
                                  images, cam, lmk, masks, va, expr7, affectnetexp, None, exprw)


        # -- detail
        if self.mode == DecaMode.DETAIL:
            detailcode = self.deca.E_detail(images)

            if training:
                if self.deca.config.detail_constrain_type == 'exchange':
                    '''
                    make sure s0, s1 is something to make shape close
                    the difference from ||so - s1|| is 
                    the later encourage s0, s1 is cloase in l2 space, but not really ensure shape will be close
                    '''
                    # this creates a per-ring random permutation. The detail exchange happens ONLY between the same
                    # identities (within the ring) but not outside (no cross-identity detail exchange)
                    new_order = np.array(
                        # [np.random.permutation(self.deca.config.train_K) + i * self.deca.config.train_K for i in range(original_batch_size)])
                        [np.random.permutation(K) + i * K for i in range(original_batch_size)])
                    new_order = new_order.flatten()
                    detailcode_new = detailcode[new_order]
                    # import ipdb; ipdb.set_trace()
                    detailcode = torch.cat([detailcode, detailcode_new], dim=0)
                    ## append new shape code data
                    shapecode = torch.cat([shapecode, shapecode], dim=0)
                    texcode = torch.cat([texcode, texcode], dim=0)
                    expcode = torch.cat([expcode, expcode], dim=0)
                    posecode = torch.cat([posecode, posecode], dim=0)
                    cam = torch.cat([cam, cam], dim=0)
                    lightcode = torch.cat([lightcode, lightcode], dim=0)
                    ## append gt
                    images = torch.cat([images, images],
                                       dim=0)  # images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])
                    lmk = torch.cat([lmk, lmk], dim=0)  # lmk = lmk.view(-1, lmk.shape[-2], lmk.shape[-1])
                    masks = torch.cat([masks, masks], dim=0)

                    if va is not None:
                        va = torch.cat([va, va], dim=0)
                    if expr7 is not None:
                        expr7 = torch.cat([expr7, expr7], dim=0)

                elif 'expression_constrain_type' in self.deca.config.keys() and \
                        self.deca.config.expression_constrain_type == 'exchange':
                    expcode, posecode, shapecode, lightcode, texcode, images, cam, lmk, masks, va, expr7, affectnetexp, detailcode, exprw = \
                        self._expression_ring_exchange(original_batch_size, K,
                                  expcode, posecode, shapecode, lightcode, texcode,
                                  images, cam, lmk, masks, va, expr7, affectnetexp, detailcode, exprw)


        codedict['shapecode'] = shapecode
        codedict['texcode'] = texcode
        codedict['expcode'] = expcode
        codedict['posecode'] = posecode
        codedict['cam'] = cam
        codedict['lightcode'] = lightcode
        if self.mode == DecaMode.DETAIL:
            codedict['detailcode'] = detailcode
        codedict['images'] = images
        if 'mask' in batch.keys():
            codedict['masks'] = masks
        if 'landmark' in batch.keys():
            codedict['lmk'] = lmk

        if 'va' in batch.keys():
            codedict['va'] = va
        if 'expr7' in batch.keys():
            codedict['expr7'] = expr7
        if 'affectnetexp' in batch.keys():
            codedict['affectnetexp'] = affectnetexp

        if 'expression_weight' in batch.keys():
            codedict['expression_weight'] = exprw

        return codedict

    def decode(self, codedict, training=True) -> dict:
        shapecode = codedict['shapecode']
        expcode = codedict['expcode']
        posecode = codedict['posecode']
        texcode = codedict['texcode']
        cam = codedict['cam']
        lightcode = codedict['lightcode']
        images = codedict['images']
        if 'masks' in codedict.keys():
            masks = codedict['masks']
        else:
            masks = None

        effective_batch_size = images.shape[0]  # this is the current batch size after all training augmentations modifications

        # FLAME - world space
        verts, landmarks2d, landmarks3d = self.deca.flame(shape_params=shapecode, expression_params=expcode,
                                                          pose_params=posecode)
        # world to camera
        trans_verts = util.batch_orth_proj(verts, cam)
        predicted_landmarks = util.batch_orth_proj(landmarks2d, cam)[:, :, :2]
        # camera to image space
        trans_verts[:, :, 1:] = -trans_verts[:, :, 1:]
        predicted_landmarks[:, :, 1:] = - predicted_landmarks[:, :, 1:]

        albedo = self.deca.flametex(texcode)

        # ------ rendering
        ops = self.deca.render(verts, trans_verts, albedo, lightcode)
        # mask
        mask_face_eye = F.grid_sample(self.deca.uv_face_eye_mask.expand(effective_batch_size, -1, -1, -1),
                                      ops['grid'].detach(),
                                      align_corners=False)
        # images
        predicted_images = ops['images']
        # predicted_images = ops['images'] * mask_face_eye * ops['alpha_images']
        # predicted_images_no_mask = ops['images'] #* mask_face_eye * ops['alpha_images']
        segmentation_type = None
        if isinstance(self.deca.config.useSeg, bool):
            if self.deca.config.useSeg:
                segmentation_type = 'gt'
            else:
                segmentation_type = 'rend'
        elif isinstance(self.deca.config.useSeg, str):
            segmentation_type = self.deca.config.useSeg
        else:
            raise RuntimeError(f"Invalid 'useSeg' type: '{type(self.deca.config.useSeg)}'")

        if segmentation_type not in ["gt", "rend", "intersection", "union"]:
            raise ValueError(f"Invalid segmentation type for masking '{segmentation_type}'")

        if masks is None: # if mask not provided, the only mask available is the rendered one
            segmentation_type = 'rend'

        elif masks.shape[-1] != predicted_images.shape[-1] or masks.shape[-2] != predicted_images.shape[-2]:
            # resize masks if need be (this is only done if configuration was changed at some point after training)
            dims = masks.ndim == 3
            if dims:
                masks = masks[:, None, :, :]
            masks = F.interpolate(masks, size=predicted_images.shape[-2:], mode='bilinear')
            if dims:
                masks = masks[:, 0, ...]

        # resize images if need be (this is only done if configuration was changed at some point after training)
        if images.shape[-1] != predicted_images.shape[-1] or images.shape[-2] != predicted_images.shape[-2]:
            ## special case only for inference time if the rendering image sizes have been changed
            images_resized = F.interpolate(images, size=predicted_images.shape[-2:], mode='bilinear')
        else:
            images_resized = images

        if segmentation_type == "gt":
            masks = masks[:, None, :, :]
        elif segmentation_type == "rend":
            masks = mask_face_eye * ops['alpha_images']
        elif segmentation_type == "intersection":
            masks = masks[:, None, :, :] * mask_face_eye * ops['alpha_images']
        elif segmentation_type == "union":
            masks = torch.max(masks[:, None, :, :],  mask_face_eye * ops['alpha_images'])
        else:
            raise RuntimeError(f"Invalid segmentation type for masking '{segmentation_type}'")


        if self.deca.config.background_from_input in [True, "input"]:
            if images.shape[-1] != predicted_images.shape[-1] or images.shape[-2] != predicted_images.shape[-2]:
                ## special case only for inference time if the rendering image sizes have been changed
                predicted_images = (1. - masks) * images_resized + masks * predicted_images
            else:
                predicted_images = (1. - masks) * images + masks * predicted_images
        elif self.deca.config.background_from_input in [False, "black"]:
            predicted_images = masks * predicted_images
        elif self.deca.config.background_from_input in ["none"]:
            predicted_images = predicted_images
        else:
            raise ValueError(f"Invalid type of background modification {self.deca.config.background_from_input}")

        if self.mode == DecaMode.DETAIL:
            detailcode = codedict['detailcode']
            uv_z = self.deca.D_detail(torch.cat([posecode[:, 3:], expcode, detailcode], dim=1))
            # render detail
            uv_detail_normals, uv_coarse_vertices = self.deca.displacement2normal(uv_z, verts, ops['normals'])
            uv_shading = self.deca.render.add_SHlight(uv_detail_normals, lightcode.detach())
            uv_texture = albedo.detach() * uv_shading
            predicted_detailed_image = F.grid_sample(uv_texture, ops['grid'].detach(), align_corners=False)
            if self.deca.config.background_from_input in [True, "input"]:
                if images.shape[-1] != predicted_images.shape[-1] or images.shape[-2] != predicted_images.shape[-2]:
                    ## special case only for inference time if the rendering image sizes have been changed
                    # images_resized = F.interpolate(images, size=predicted_images.shape[-2:], mode='bilinear')
                    ## before bugfix
                    # predicted_images = (1. - masks) * images_resized + masks * predicted_images
                    ## after bugfix
                    predicted_detailed_image = (1. - masks) * images_resized + masks * predicted_detailed_image
                else:
                    predicted_detailed_image = (1. - masks) * images + masks * predicted_detailed_image
            elif self.deca.config.background_from_input in [False, "black"]:
                predicted_detailed_image = masks * predicted_detailed_image
            elif self.deca.config.background_from_input in ["none"]:
                predicted_detailed_image = predicted_detailed_image
            else:
                raise ValueError(f"Invalid type of background modification {self.deca.config.background_from_input}")


            # --- extract texture
            uv_pverts = self.deca.render.world2uv(trans_verts).detach()
            uv_gt = F.grid_sample(torch.cat([images_resized, masks], dim=1), uv_pverts.permute(0, 2, 3, 1)[:, :, :, :2],
                                  mode='bilinear')
            uv_texture_gt = uv_gt[:, :3, :, :].detach()
            uv_mask_gt = uv_gt[:, 3:, :, :].detach()
            # self-occlusion
            normals = util.vertex_normals(trans_verts, self.deca.render.faces.expand(effective_batch_size, -1, -1))
            uv_pnorm = self.deca.render.world2uv(normals)

            uv_mask = (uv_pnorm[:, -1, :, :] < -0.05).float().detach()
            uv_mask = uv_mask[:, None, :, :]
            ## combine masks
            uv_vis_mask = uv_mask_gt * uv_mask * self.deca.uv_face_eye_mask
        else:
            uv_detail_normals = None
            predicted_detailed_image = None


        ## NEURAL RENDERING
        # if hasattr(self, 'image_translator') and self.image_translator is not None:
        if self.deca._has_neural_rendering():
            predicted_translated_image = self.deca.image_translator(
                {
                    "input_image" : predicted_images,
                    "ref_image" : images,
                    "target_domain" : torch.tensor([0]*predicted_images.shape[0],
                                                   dtype=torch.int64, device=predicted_images.device)
                }
            )

            if self.mode == DecaMode.DETAIL:
                predicted_detailed_translated_image = self.deca.image_translator(
                        {
                            "input_image" : predicted_detailed_image,
                            "ref_image" : images,
                            "target_domain" : torch.tensor([0]*predicted_detailed_image.shape[0],
                                                           dtype=torch.int64, device=predicted_detailed_image.device)
                        }
                    )
                translated_uv = F.grid_sample(torch.cat([predicted_detailed_translated_image, masks], dim=1), uv_pverts.permute(0, 2, 3, 1)[:, :, :, :2],
                                      mode='bilinear')
                translated_uv_texture = translated_uv[:, :3, :, :].detach()

            else:
                predicted_detailed_translated_image = None

                translated_uv_texture = None
                # no need in coarse mode
                # translated_uv = F.grid_sample(torch.cat([predicted_translated_image, masks], dim=1), uv_pverts.permute(0, 2, 3, 1)[:, :, :, :2],
                #                       mode='bilinear')
                # translated_uv_texture = translated_uv_gt[:, :3, :, :].detach()
        else:
            predicted_translated_image = None
            predicted_detailed_translated_image = None
            translated_uv_texture = None

        if self.emotion_mlp is not None:
            codedict = self.emotion_mlp(codedict, "emo_mlp_")

        # populate the value dict for metric computation/visualization
        codedict['predicted_images'] = predicted_images
        codedict['predicted_detailed_image'] = predicted_detailed_image
        codedict['predicted_translated_image'] = predicted_translated_image
        codedict['verts'] = verts
        codedict['albedo'] = albedo
        codedict['mask_face_eye'] = mask_face_eye
        codedict['landmarks2d'] = landmarks2d
        codedict['landmarks3d'] = landmarks3d
        codedict['predicted_landmarks'] = predicted_landmarks
        codedict['trans_verts'] = trans_verts
        codedict['ops'] = ops
        codedict['masks'] = masks
        codedict['normals'] = ops['normals']

        if self.mode == DecaMode.DETAIL:
            codedict['predicted_detailed_translated_image'] = predicted_detailed_translated_image
            codedict['translated_uv_texture'] = translated_uv_texture
            codedict['uv_texture_gt'] = uv_texture_gt
            codedict['uv_texture'] = uv_texture
            codedict['uv_detail_normals'] = uv_detail_normals
            codedict['uv_z'] = uv_z
            codedict['uv_shading'] = uv_shading
            codedict['uv_vis_mask'] = uv_vis_mask
            codedict['uv_mask'] = uv_mask
            codedict['displacement_map'] = uv_z + self.deca.fixed_uv_dis[None, None, :, :]

        return codedict

    def _compute_emotion_loss(self, images, predicted_images, loss_dict, metric_dict, prefix, va=None, expr7=None, with_grad=True):
        def loss_or_metric(name, loss, is_loss):
            if not is_loss:
                metric_dict[name] = loss
            else:
                loss_dict[name] = loss

        # if self.deca.config.use_emonet_loss:
        if with_grad:
            d = loss_dict
            emo_feat_loss_1, emo_feat_loss_2, valence_loss, arousal_loss, expression_loss = \
                self.emonet_loss.compute_loss(images, predicted_images)
        else:
            d = metric_dict
            with torch.no_grad():
                emo_feat_loss_1, emo_feat_loss_2, valence_loss, arousal_loss, expression_loss = \
                    self.emonet_loss.compute_loss(images, predicted_images)



        # EmoNet self-consistency loss terms
        if emo_feat_loss_1 is not None:
            loss_or_metric(prefix + '_emonet_feat_1_L1', emo_feat_loss_1 * self.deca.config.emonet_weight,
                           self.deca.config.use_emonet_feat_1 and self.deca.config.use_emonet_loss)
        loss_or_metric(prefix + '_emonet_feat_2_L1', emo_feat_loss_2 * self.deca.config.emonet_weight,
                       self.deca.config.use_emonet_feat_2 and self.deca.config.use_emonet_loss)
        loss_or_metric(prefix + '_emonet_valence_L1', valence_loss * self.deca.config.emonet_weight,
                       self.deca.config.use_emonet_valence and self.deca.config.use_emonet_loss)
        loss_or_metric(prefix + '_emonet_arousal_L1', arousal_loss * self.deca.config.emonet_weight,
                       self.deca.config.use_emonet_arousal and self.deca.config.use_emonet_loss)
        # loss_or_metric(prefix + 'emonet_expression_KL', expression_loss * self.deca.config.emonet_weight) # KL seems to be causing NaN's
        loss_or_metric(prefix + '_emonet_expression_L1',expression_loss * self.deca.config.emonet_weight,
                       self.deca.config.use_emonet_expression and self.deca.config.use_emonet_loss)
        loss_or_metric(prefix + '_emonet_combined', ((emo_feat_loss_1 if emo_feat_loss_1 is not None else 0)
                                                     + emo_feat_loss_2 + valence_loss + arousal_loss + expression_loss) * self.deca.config.emonet_weight,
                       self.deca.config.use_emonet_combined and self.deca.config.use_emonet_loss)

        # Log also the VA
        metric_dict[prefix + "_valence_input"] = self.emonet_loss.input_emotion['valence'].mean().detach()
        metric_dict[prefix + "_valence_output"] = self.emonet_loss.output_emotion['valence'].mean().detach()
        metric_dict[prefix + "_arousal_input"] = self.emonet_loss.input_emotion['arousal'].mean().detach()
        metric_dict[prefix + "_arousal_output"] = self.emonet_loss.output_emotion['arousal'].mean().detach()

        input_ex = self.emonet_loss.input_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification'].detach().cpu().numpy()
        input_ex = np.argmax(input_ex, axis=1).mean()
        output_ex = self.emonet_loss.output_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification'].detach().cpu().numpy()
        output_ex = np.argmax(output_ex, axis=1).mean()
        metric_dict[prefix + "_expression_input"] = torch.tensor(input_ex, device=self.device)
        metric_dict[prefix + "_expression_output"] = torch.tensor(output_ex, device=self.device)

        # # GT emotion loss terms
        # if self.deca.config.use_gt_emotion_loss:
        #     d = loss_dict
        # else:
        #     d = metric_dict

        # TODO: uncomment this after you handle the case when certain entries are NaN (GT missing, not a bug)
        # if va is not None:
        #     d[prefix + 'emo_sup_val_L1'] = F.l1_loss(self.emonet_loss.output_emotion['valence'], va[:, 0]) \
        #                                    * self.deca.config.gt_emotion_reg
        #     d[prefix + 'emo_sup_ar_L1'] = F.l1_loss(self.emonet_loss.output_emotion['arousal'], va[:, 1]) \
        #                                   * self.deca.config.gt_emotion_reg
        #
        #     metric_dict[prefix + "_valence_gt"] = va[:, 0].mean().detach()
        #     metric_dict[prefix + "_arousal_gt"] = va[:, 1].mean().detach()
        #
        # if expr7 is not None:
        #     affectnet_gt = [expr7_to_affect_net(int(expr7[i])).value for i in range(len(expr7))]
        #     affectnet_gt = torch.tensor(np.array(affectnet_gt), device=self.device, dtype=torch.long)
        #     d[prefix + '_emo_sup_expr_CE'] = F.cross_entropy(self.emonet_loss.output_emotion['expression'], affectnet_gt) * self.deca.config.gt_emotion_reg
        #     metric_dict[prefix + "_expr_gt"] = affectnet_gt.mean().detach()


    def _metric_or_loss(self, loss_dict, metric_dict, is_loss):
        if is_loss:
            d = loss_dict
        else:
            d = metric_dict
        return d


    def _compute_loss(self, codedict, batch, training=True, testing=False) -> (dict, dict):
        #### ----------------------- Losses
        losses = {}
        metrics = {}

        predicted_landmarks = codedict["predicted_landmarks"]
        if "lmk" in codedict.keys():
            lmk = codedict["lmk"]
        else:
            lmk = None

        if "masks" in codedict.keys():
            masks = codedict["masks"]
        else:
            masks = None

        batch_size = codedict["predicted_images"].shape[0]

        use_geom_losses = 'use_geometric_losses_expression_exchange' in self.deca.config.keys() and \
            self.deca.config.use_geometric_losses_expression_exchange

        if training and 'expression_constrain_type' in self.deca.config.keys() \
            and self.deca.config.expression_constrain_type == 'exchange' \
            and (self.deca.mode == DecaMode.COARSE or self.deca.config.train_coarse) \
            and (not use_geom_losses):
            if batch_size % 2 != 0:
                raise RuntimeError("The batch size should be even because it should have "
                                   f"got doubled in expression ring exchange. Instead it was odd: {batch_size}")
            # THIS IS DONE BECAUSE LANDMARK AND PHOTOMETRIC LOSSES MAKE NO SENSE FOR EXPRESSION EXCHANGE
            geom_losses_idxs = batch_size // 2

        else:
            geom_losses_idxs = batch_size

        predicted_images = codedict["predicted_images"]
        images = codedict["images"]
        lightcode = codedict["lightcode"]
        albedo = codedict["albedo"]
        mask_face_eye = codedict["mask_face_eye"]
        shapecode = codedict["shapecode"]
        expcode = codedict["expcode"]
        texcode = codedict["texcode"]
        ops = codedict["ops"]

        if 'va' in codedict:
            va = codedict['va']
            va = va.view(-1, va.shape[-1])
        else:
            va = None

        if 'expr7' in codedict:
            expr7 = codedict['expr7']
            expr7 = expr7.view(-1, expr7.shape[-1])
        else:
            expr7 = None


        if self.mode == DecaMode.DETAIL:
            uv_texture = codedict["uv_texture"]
            uv_texture_gt = codedict["uv_texture_gt"]


        ## COARSE loss only
        if self.mode == DecaMode.COARSE or (self.mode == DecaMode.DETAIL and self.deca.config.train_coarse):

            # landmark losses (only useful if coarse model is being trained
            # if training or lmk is not None:
            if lmk is not None:
                # if self.deca.config.use_landmarks:
                #     d = losses
                # else:
                #     d = metrics
                d = self._metric_or_loss(losses, metrics, self.deca.config.use_landmarks)

                if self.deca.config.useWlmk:
                    d['landmark'] = \
                        lossfunc.weighted_landmark_loss(predicted_landmarks[:geom_losses_idxs, ...], lmk[:geom_losses_idxs, ...]) * self.deca.config.lmk_weight
                else:
                    d['landmark'] = \
                        lossfunc.landmark_loss(predicted_landmarks[:geom_losses_idxs, ...], lmk[:geom_losses_idxs, ...]) * self.deca.config.lmk_weight
                # losses['eye_distance'] = lossfunc.eyed_loss(predicted_landmarks, lmk) * self.deca.config.lmk_weight * 2
                d['eye_distance'] = lossfunc.eyed_loss(predicted_landmarks[:geom_losses_idxs, ...], lmk[:geom_losses_idxs, ...]) * self.deca.config.eyed
                d['lip_distance'] = lossfunc.lipd_loss(predicted_landmarks[:geom_losses_idxs, ...], lmk[:geom_losses_idxs, ...]) * self.deca.config.lipd
                #TODO: fix this on the next iteration lipd_loss
                # d['lip_distance'] = lossfunc.lipd_loss(predicted_landmarks, lmk) * self.deca.config.lipd

            # photometric loss
            # if training or masks is not None:
            if masks is not None:
                # if self.deca.config.use_photometric:
                #     d = losses
                # else:
                #     d = metrics
                # d['photometric_texture'] = (masks * (predicted_images - images).abs()).mean() * self.deca.config.photow
                self._metric_or_loss(losses, metrics, self.deca.config.use_photometric)['photometric_texture'] = \
                    (masks[:geom_losses_idxs, ...] * (predicted_images[:geom_losses_idxs, ...] - images[:geom_losses_idxs, ...]).abs()).mean() * self.deca.config.photow

                if self.deca.vgg_loss is not None:
                    vggl, _ = self.deca.vgg_loss(
                        masks[:geom_losses_idxs, ...] * images[:geom_losses_idxs, ...], # masked input image
                        masks[:geom_losses_idxs, ...] * predicted_images[:geom_losses_idxs, ...], # masked output image
                    )
                    self._metric_or_loss(losses, metrics, self.deca.config.use_vgg)['vgg'] = vggl * self.deca.config.vggw

                if self.deca._has_neural_rendering():
                    predicted_translated_image = codedict["predicted_translated_image"]
                    photometric_translated = (masks[:geom_losses_idxs, ...] * (
                            predicted_translated_image[:geom_losses_idxs, ...] -
                            images[:geom_losses_idxs, ...]).abs()).mean() * self.deca.config.photow
                    if self.deca.config.use_photometric:
                        losses['photometric_translated_texture'] = photometric_translated
                    else:
                        metrics['photometric_translated_texture'] = photometric_translated

                    if self.deca.vgg_loss is not None:
                        vggl, _ = self.deca.vgg_loss(
                            masks[:geom_losses_idxs, ...] * images[:geom_losses_idxs, ...],  # masked input image
                            masks[:geom_losses_idxs, ...] * predicted_translated_image[:geom_losses_idxs, ...],
                            # masked output image
                        )
                        self._metric_or_loss(losses, metrics, self.deca.config.use_vgg)['vgg_translated'] = vggl * self.deca.config.vggw

            else:
                raise ValueError("Is this line ever reached?")


            # if self.deca.config.idw > 1e-3:
            if self.deca.id_loss is not None:
                shading_images = self.deca.render.add_SHlight(ops['normal_images'], lightcode.detach())
                albedo_images = F.grid_sample(albedo.detach(), ops['grid'], align_corners=False)
                overlay = albedo_images * shading_images * mask_face_eye + images * (1 - mask_face_eye)
                losses['identity'] = self.deca.id_loss(overlay, images) * self.deca.config.idw

            losses['shape_reg'] = (torch.sum(shapecode ** 2) / 2) * self.deca.config.shape_reg
            losses['expression_reg'] = (torch.sum(expcode ** 2) / 2) * self.deca.config.exp_reg
            losses['tex_reg'] = (torch.sum(texcode ** 2) / 2) * self.deca.config.tex_reg
            losses['light_reg'] = ((torch.mean(lightcode, dim=2)[:, :,
                                    None] - lightcode) ** 2).mean() * self.deca.config.light_reg

            if self.emonet_loss is not None:
                # with torch.no_grad():

                self._compute_emotion_loss(images, predicted_images, losses, metrics, "coarse",
                                           va, expr7, with_grad=self.deca.config.use_emonet_loss and not self.deca._has_neural_rendering())

                codedict["coarse_valence_input"] = self.emonet_loss.input_emotion['valence']
                codedict["coarse_arousal_input"] = self.emonet_loss.input_emotion['arousal']
                codedict["coarse_expression_input"] = self.emonet_loss.input_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification']
                codedict["coarse_valence_output"] = self.emonet_loss.output_emotion['valence']
                codedict["coarse_arousal_output"] = self.emonet_loss.output_emotion['arousal']
                codedict["coarse_expression_output"] = self.emonet_loss.output_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification']

                if va is not None:
                    codedict["coarse_valence_gt"] = va[:, 0]
                    codedict["coarse_arousal_gt"] = va[:, 1]
                if expr7 is not None:
                    codedict["coarse_expression_gt"] = expr7

                if self.deca._has_neural_rendering():
                    #TODO possible to make this more GPU efficient by not recomputing emotion for input image
                    self._compute_emotion_loss(images, predicted_translated_image, losses, metrics, "coarse_translated",
                                               va, expr7,
                                               with_grad=self.deca.config.use_emonet_loss and self.deca._has_neural_rendering())

                    # codedict["coarse_valence_input"] = self.emonet_loss.input_emotion['valence']
                    # codedict["coarse_arousal_input"] = self.emonet_loss.input_emotion['arousal']
                    # codedict["coarse_expression_input"] = self.emonet_loss.input_emotion['expression']
                    codedict["coarse_translated_valence_output"] = self.emonet_loss.output_emotion['valence']
                    codedict["coarse_translated_arousal_output"] = self.emonet_loss.output_emotion['arousal']
                    codedict["coarse_translated_expression_output"] = self.emonet_loss.output_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification']

        ## DETAIL loss only
        if self.mode == DecaMode.DETAIL:
            predicted_detailed_image = codedict["predicted_detailed_image"]
            uv_z = codedict["uv_z"] # UV displacement map
            uv_shading = codedict["uv_shading"]
            uv_vis_mask = codedict["uv_vis_mask"] # uv_mask of what is visible

            photometric_detailed = (masks[:geom_losses_idxs, ...] * (
                    predicted_detailed_image[:geom_losses_idxs, ...] -
                    images[:geom_losses_idxs, ...]).abs()).mean() * self.deca.config.photow

            if self.deca.config.use_detailed_photo:
                losses['photometric_detailed_texture'] = photometric_detailed
            else:
                metrics['photometric_detailed_texture'] = photometric_detailed

            if self.deca.vgg_loss is not None:
                vggl, _ = self.deca.vgg_loss(
                    masks[:geom_losses_idxs, ...] * images[:geom_losses_idxs, ...],  # masked input image
                    masks[:geom_losses_idxs, ...] * predicted_detailed_image[:geom_losses_idxs, ...],
                    # masked output image
                )
                self._metric_or_loss(losses, metrics, self.deca.config.use_vgg)['vgg_detailed'] = vggl * self.deca.config.vggw

            if self.deca._has_neural_rendering():
                predicted_detailed_translated_image = codedict["predicted_detailed_translated_image"]
                photometric_detailed_translated = (masks[:geom_losses_idxs, ...] * (
                        predicted_detailed_translated_image[:geom_losses_idxs, ...] - images[:geom_losses_idxs,
                                                                           ...]).abs()).mean() * self.deca.config.photow
                if self.deca.config.use_detailed_photo:
                    losses['photometric_translated_detailed_texture'] = photometric_detailed_translated
                else:
                    metrics['photometric_translated_detailed_texture'] = photometric_detailed_translated

                if self.deca.vgg_loss is not None:
                    vggl, _ = self.deca.vgg_loss(
                        masks[:geom_losses_idxs, ...] * images[:geom_losses_idxs, ...],  # masked input image
                        masks[:geom_losses_idxs, ...] * photometric_detailed_translated[:geom_losses_idxs, ...],
                        # masked output image
                    )
                    self._metric_or_loss(losses, metrics, self.deca.config.use_vgg)[
                        'vgg_detailed_translated'] =  vggl * self.deca.config.vggw

            if self.emonet_loss is not None:
                self._compute_emotion_loss(images, predicted_detailed_image, losses, metrics, "detail",
                                           with_grad=self.deca.config.use_emonet_loss and not self.deca._has_neural_rendering())
                codedict["detail_valence_input"] = self.emonet_loss.input_emotion['valence']
                codedict["detail_arousal_input"] = self.emonet_loss.input_emotion['arousal']
                codedict["detail_expression_input"] = self.emonet_loss.input_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification']
                codedict["detail_valence_output"] = self.emonet_loss.output_emotion['valence']
                codedict["detail_arousal_output"] = self.emonet_loss.output_emotion['arousal']
                codedict["detail_expression_output"] = self.emonet_loss.output_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification']

                if va is not None:
                    codedict["detail_valence_gt"] = va[:,0]
                    codedict["detail_arousal_gt"] = va[:,1]
                if expr7 is not None:
                    codedict["detail_expression_gt"] = expr7


                if self.deca._has_neural_rendering():
                    #TODO possible to make this more GPU efficient by not recomputing emotion for input image
                    self._compute_emotion_loss(images, predicted_detailed_translated_image,
                                               losses, metrics, "detail_translated",
                                               va, expr7,
                                               with_grad= self.deca.config.use_emonet_loss and self.deca._has_neural_rendering())

                    # codedict["coarse_valence_input"] = self.emonet_loss.input_emotion['valence']
                    # codedict["coarse_arousal_input"] = self.emonet_loss.input_emotion['arousal']
                    # codedict["coarse_expression_input"] = self.emonet_loss.input_emotion['expression']
                    codedict["detail_translated_valence_output"] = self.emonet_loss.output_emotion['valence']
                    codedict["detail_translated_arousal_output"] = self.emonet_loss.output_emotion['arousal']
                    codedict["detail_translated_expression_output"] = self.emonet_loss.output_emotion['expression' if 'expression' in self.emonet_loss.input_emotion.keys() else 'expr_classification']

            for pi in range(3):  # self.deca.face_attr_mask.shape[0]):
                if self.deca.config.sfsw[pi] != 0:
                    # if pi==0:
                    new_size = 256
                    # else:
                    #     new_size = 128
                    # if self.deca.config.uv_size != 256:
                    #     new_size = 128
                    uv_texture_patch = F.interpolate(
                        uv_texture[:geom_losses_idxs, :, self.deca.face_attr_mask[pi][2]:self.deca.face_attr_mask[pi][3],
                        self.deca.face_attr_mask[pi][0]:self.deca.face_attr_mask[pi][1]],
                        [new_size, new_size], mode='bilinear')
                    uv_texture_gt_patch = F.interpolate(
                        uv_texture_gt[:geom_losses_idxs, :, self.deca.face_attr_mask[pi][2]:self.deca.face_attr_mask[pi][3],
                        self.deca.face_attr_mask[pi][0]:self.deca.face_attr_mask[pi][1]], [new_size, new_size],
                        mode='bilinear')
                    uv_vis_mask_patch = F.interpolate(
                        uv_vis_mask[:geom_losses_idxs, :, self.deca.face_attr_mask[pi][2]:self.deca.face_attr_mask[pi][3],
                        self.deca.face_attr_mask[pi][0]:self.deca.face_attr_mask[pi][1]],
                        [new_size, new_size], mode='bilinear')

                    detail_l1 = (uv_texture_patch * uv_vis_mask_patch - uv_texture_gt_patch * uv_vis_mask_patch).abs().mean() * \
                                                        self.deca.config.sfsw[pi]
                    if self.deca.config.use_detail_l1 and not self.deca._has_neural_rendering():
                        losses['detail_l1_{}'.format(pi)] = detail_l1
                    else:
                        metrics['detail_l1_{}'.format(pi)] = detail_l1

                    if self.deca.config.use_detail_mrf and not self.deca._has_neural_rendering():
                        mrf = self.deca.perceptual_loss(uv_texture_patch * uv_vis_mask_patch,
                                                        uv_texture_gt_patch * uv_vis_mask_patch) * \
                                                        self.deca.config.sfsw[pi] * self.deca.config.mrfwr
                        losses['detail_mrf_{}'.format(pi)] = mrf
                    else:
                        with torch.no_grad():
                            mrf = self.deca.perceptual_loss(uv_texture_patch * uv_vis_mask_patch,
                                                            uv_texture_gt_patch * uv_vis_mask_patch) * \
                                  self.deca.config.sfsw[pi] * self.deca.config.mrfwr
                            metrics['detail_mrf_{}'.format(pi)] = mrf

                    if self.deca._has_neural_rendering():
                        # raise NotImplementedError("Gotta implement the texture extraction first.")
                        translated_uv_texture = codedict["translated_uv_texture"]
                        translated_uv_texture_patch = F.interpolate(
                            translated_uv_texture[:geom_losses_idxs, :,
                            self.deca.face_attr_mask[pi][2]:self.deca.face_attr_mask[pi][3],
                            self.deca.face_attr_mask[pi][0]:self.deca.face_attr_mask[pi][1]],
                            [new_size, new_size], mode='bilinear')

                        translated_detail_l1 = (translated_uv_texture_patch * uv_vis_mask_patch
                                     - uv_texture_gt_patch * uv_vis_mask_patch).abs().mean() * \
                                    self.deca.config.sfsw[pi]

                        if self.deca.config.use_detail_l1:
                            losses['detail_translated_l1_{}'.format(pi)] = translated_detail_l1
                        else:
                            metrics['detail_translated_l1_{}'.format(pi)] = translated_detail_l1

                        if self.deca.config.use_detail_mrf:
                            translated_mrf = self.deca.perceptual_loss(translated_uv_texture_patch * uv_vis_mask_patch,
                                                            uv_texture_gt_patch * uv_vis_mask_patch) * \
                                  self.deca.config.sfsw[pi] * self.deca.config.mrfwr
                            losses['detail_translated_mrf_{}'.format(pi)] = translated_mrf
                        else:
                            with torch.no_grad():
                                mrf = self.deca.perceptual_loss(translated_uv_texture_patch * uv_vis_mask_patch,
                                                                uv_texture_gt_patch * uv_vis_mask_patch) * \
                                      self.deca.config.sfsw[pi] * self.deca.config.mrfwr
                                metrics['detail_translated_mrf_{}'.format(pi)] = mrf
                # Old piece of debug code. Good to delete.
                # if pi == 2:
                #     uv_texture_gt_patch_ = uv_texture_gt_patch
                #     uv_texture_patch_ = uv_texture_patch
                #     uv_vis_mask_patch_ = uv_vis_mask_patch

            losses['z_reg'] = torch.mean(uv_z.abs()) * self.deca.config.zregw
            losses['z_diff'] = lossfunc.shading_smooth_loss(uv_shading) * self.deca.config.zdiffw
            nonvis_mask = (1 - util.binary_erosion(uv_vis_mask))
            losses['z_sym'] = (nonvis_mask * (uv_z - torch.flip(uv_z, [-1]).detach()).abs()).sum() * self.deca.config.zsymw

        if self.emotion_mlp is not None:# and not testing:
            mlp_losses, mlp_metrics = self.emotion_mlp.compute_loss(
                codedict, batch, training=training, pred_prefix="emo_mlp_")
            for key in mlp_losses.keys():
                if key in losses.keys():
                    raise RuntimeError(f"Duplicate loss label {key}")
                losses[key] = self.deca.config.mlp_emotion_predictor_weight * mlp_losses[key]
            for key in mlp_metrics.keys():
                if key in metrics.keys():
                    raise RuntimeError(f"Duplicate metric label {key}")
                # let's report the metrics (which are a superset of losses when it comes to EmoMLP) without the weight,
                # it's hard to plot the metrics otherwise
                metrics[key] = mlp_metrics[key]
                # metrics[key] = self.deca.config.mlp_emotion_predictor_weight * mlp_metrics[key]

        # else:
        #     uv_texture_gt_patch_ = None
        #     uv_texture_patch_ = None
        #     uv_vis_mask_patch_ = None

        return losses, metrics

    def compute_loss(self, values, batch, training=True, testing=False) -> (dict, dict):
        """
        training should be set to true when calling from training_step only
        """
        losses, metrics = self._compute_loss(values, batch, training=training, testing=testing)

        all_loss = 0.
        losses_key = losses.keys()
        for key in losses_key:
            all_loss = all_loss + losses[key]
        # losses['all_loss'] = all_loss
        losses = {'loss_' + key: value for key, value in losses.items()} # add prefix loss for better logging
        losses['loss'] = all_loss

        # add metrics that do not effect the loss function (if any)
        for key in metrics.keys():
            losses['metric_' + key] = metrics[key]
        return losses

    def _val_to_be_logged(self, d):
        if not hasattr(self, 'val_dict_list'):
            self.val_dict_list = []
        self.val_dict_list += [d]

    def _train_to_be_logged(self, d):
        if not hasattr(self, 'train_dict_list'):
            self.train_dict_list = []
        self.train_dict_list += [d]

    def validation_step(self, batch, batch_idx, dataloader_idx=None):
        with torch.no_grad():
            values = self.encode(batch, training=False)
            values = self.decode(values, training=False)
            losses_and_metrics = self.compute_loss(values, batch, training=False)
        #### self.log_dict(losses_and_metrics, on_step=False, on_epoch=True)
        # prefix = str(self.mode.name).lower()
        prefix = self._get_logging_prefix()

        # if dataloader_idx is not None:
        #     dataloader_str = str(dataloader_idx) + "_"
        # else:
        dataloader_str = ''

        stage_str = dataloader_str + 'val_'

        # losses_and_metrics_to_log = {prefix + dataloader_str +'_val_' + key: value.detach().cpu() for key, value in losses_and_metrics.items()}
        # losses_and_metrics_to_log = {prefix + '_' + stage_str + key: value.detach() for key, value in losses_and_metrics.items()}
        losses_and_metrics_to_log = {prefix + '_' + stage_str + key: value.detach().cpu().item() for key, value in losses_and_metrics.items()}
        losses_and_metrics_to_log[prefix + '_' + stage_str + 'epoch'] = self.current_epoch
        # losses_and_metrics_to_log[prefix + '_' + stage_str + 'epoch'] = torch.tensor(self.current_epoch, device=self.device)
        # log val_loss also without any prefix for a model checkpoint to track it
        losses_and_metrics_to_log[stage_str + 'loss'] = losses_and_metrics_to_log[prefix + '_' + stage_str + 'loss']

        losses_and_metrics_to_log[prefix + '_' + stage_str + 'step'] = self.global_step
        losses_and_metrics_to_log[prefix + '_' + stage_str + 'batch_idx'] = batch_idx
        losses_and_metrics_to_log[stage_str + 'step'] = self.global_step
        losses_and_metrics_to_log[stage_str + 'batch_idx'] = batch_idx

        losses_and_metrics_to_log[prefix + '_' + stage_str + 'mem_usage'] = self.process.memory_info().rss
        losses_and_metrics_to_log[stage_str + 'mem_usage'] = self.process.memory_info().rss
        # self._val_to_be_logged(losses_and_metrics_to_log)
        if self.deca.config.val_vis_frequency > 0:
            if batch_idx % self.deca.config.val_vis_frequency == 0:
                uv_detail_normals = None
                if 'uv_detail_normals' in values.keys():
                    uv_detail_normals = values['uv_detail_normals']
                visualizations, grid_image = self._visualization_checkpoint(values['verts'], values['trans_verts'], values['ops'],
                                               uv_detail_normals, values, batch_idx, stage_str[:-1], prefix)
                vis_dict = self._create_visualizations_to_log(stage_str[:-1], visualizations, values, batch_idx, indices=0, dataloader_idx=dataloader_idx)
                # image = Image(grid_image, caption="full visualization")
                # vis_dict[prefix + '_val_' + "visualization"] = image
                if isinstance(self.logger, WandbLogger):
                    self.logger.log_metrics(vis_dict)
                # self.logger.experiment.log(vis_dict) #, step=self.global_step)

        if self.logger is not None:
            self.log_dict(losses_and_metrics_to_log, on_step=False, on_epoch=True, sync_dist=True) # log per epoch # recommended
        # self.log_dict(losses_and_metrics_to_log, on_step=True, on_epoch=False) # log per step
        # self.log_dict(losses_and_metrics_to_log, on_step=True, on_epoch=True) # log per both
        # return losses_and_metrics
        return None

    def _get_logging_prefix(self):
        prefix = self.stage_name + str(self.mode.name).lower()
        return prefix

    def test_step(self, batch, batch_idx, dataloader_idx=None):
        prefix = self._get_logging_prefix()
        losses_and_metrics_to_log = {}

        # if dataloader_idx is not None:
        #     dataloader_str = str(dataloader_idx) + "_"
        # else:
        dataloader_str = ''
        stage_str = dataloader_str + 'test_'

        with torch.no_grad():
            values = self.encode(batch, training=False)
            values = self.decode(values, training=False)
            if 'mask' in batch.keys():
                losses_and_metrics = self.compute_loss(values, batch, training=False, testing=True)
                # losses_and_metrics_to_log = {prefix + '_' + stage_str + key: value.detach().cpu() for key, value in losses_and_metrics.items()}
                losses_and_metrics_to_log = {prefix + '_' + stage_str + key: value.detach().cpu().item() for key, value in losses_and_metrics.items()}
            else:
                losses_and_metric = None

        # losses_and_metrics_to_log[prefix + '_' + stage_str + 'epoch'] = self.current_epoch
        # losses_and_metrics_to_log[prefix + '_' + stage_str + 'epoch'] = torch.tensor(self.current_epoch, device=self.device)
        # losses_and_metrics_to_log[prefix + '_' + stage_str + 'step'] = torch.tensor(self.global_step, device=self.device)
        # losses_and_metrics_to_log[prefix + '_' + stage_str + 'batch_idx'] = torch.tensor(batch_idx, device=self.device)
        # losses_and_metrics_to_log[stage_str + 'epoch'] = torch.tensor(self.current_epoch, device=self.device)
        # losses_and_metrics_to_log[stage_str + 'step'] = torch.tensor(self.global_step, device=self.device)
        # losses_and_metrics_to_log[stage_str + 'batch_idx'] = torch.tensor(batch_idx, device=self.device)
        losses_and_metrics_to_log[prefix + '_' + stage_str + 'epoch'] = self.current_epoch
        losses_and_metrics_to_log[prefix + '_' + stage_str + 'step'] = self.global_step
        losses_and_metrics_to_log[prefix + '_' + stage_str + 'batch_idx'] = batch_idx
        losses_and_metrics_to_log[prefix + '_' + stage_str + 'mem_usage'] = self.process.memory_info().rss
        losses_and_metrics_to_log[stage_str + 'epoch'] = self.current_epoch
        losses_and_metrics_to_log[stage_str + 'step'] = self.global_step
        losses_and_metrics_to_log[stage_str + 'batch_idx'] = batch_idx
        losses_and_metrics_to_log[stage_str + 'mem_usage'] = self.process.memory_info().rss

        # if self.global_step % 200 == 0:
        uv_detail_normals = None
        if 'uv_detail_normals' in values.keys():
            uv_detail_normals = values['uv_detail_normals']

        if self.deca.config.test_vis_frequency > 0:
            if batch_idx % self.deca.config.test_vis_frequency == 0:
                visualizations, grid_image = self._visualization_checkpoint(values['verts'], values['trans_verts'], values['ops'],
                                               uv_detail_normals, values, self.global_step, stage_str[:-1], prefix)
                visdict = self._create_visualizations_to_log(stage_str[:-1], visualizations, values, batch_idx, indices=0, dataloader_idx=dataloader_idx)
                # image = Image(grid_image, caption="full visualization")
                # visdict[ prefix + '_' + stage_str + "visualization"] = image
                if isinstance(self.logger, WandbLogger):
                    self.logger.log_metrics(visdict)#, step=self.global_step)
        if self.logger is not None:
            self.logger.log_metrics(losses_and_metrics_to_log)
        return None

    @property
    def process(self):
        if not hasattr(self,"process_"):
            import psutil
            self.process_ = psutil.Process(os.getpid())
        return self.process_


    def training_step(self, batch, batch_idx): #, debug=True):
        values = self.encode(batch, training=True)
        values = self.decode(values, training=True)
        losses_and_metrics = self.compute_loss(values, batch, training=True)

        uv_detail_normals = None
        if 'uv_detail_normals' in values.keys():
            uv_detail_normals = values['uv_detail_normals']

        # prefix = str(self.mode.name).lower()
        prefix = self._get_logging_prefix()
        # losses_and_metrics_to_log = {prefix + '_train_' + key: value.detach().cpu() for key, value in losses_and_metrics.items()}
        # losses_and_metrics_to_log = {prefix + '_train_' + key: value.detach() for key, value in losses_and_metrics.items()}
        losses_and_metrics_to_log = {prefix + '_train_' + key: value.detach().cpu().item() for key, value in losses_and_metrics.items()}
        # losses_and_metrics_to_log[prefix + '_train_' + 'epoch'] = torch.tensor(self.current_epoch, device=self.device)
        losses_and_metrics_to_log[prefix + '_train_' + 'epoch'] = self.current_epoch
        losses_and_metrics_to_log[prefix + '_train_' + 'step'] = self.global_step
        losses_and_metrics_to_log[prefix + '_train_' + 'batch_idx'] = batch_idx
        losses_and_metrics_to_log[prefix + '_' + "train_" + 'mem_usage'] = self.process.memory_info().rss

        # losses_and_metrics_to_log['train_' + 'epoch'] = torch.tensor(self.current_epoch, device=self.device)
        losses_and_metrics_to_log['train_' + 'epoch'] = self.current_epoch
        losses_and_metrics_to_log['train_' + 'step'] = self.global_step
        losses_and_metrics_to_log['train_' + 'batch_idx'] = batch_idx

        losses_and_metrics_to_log["train_" + 'mem_usage'] = self.process.memory_info().rss

        # log loss also without any prefix for a model checkpoint to track it
        losses_and_metrics_to_log['loss'] = losses_and_metrics_to_log[prefix + '_train_loss']

        if self.deca.config.train_vis_frequency > 0:
            if self.global_step % self.deca.config.train_vis_frequency == 0:
                visualizations, grid_image = self._visualization_checkpoint(values['verts'], values['trans_verts'], values['ops'],
                                               uv_detail_normals, values, batch_idx, "train", prefix)
                visdict = self._create_visualizations_to_log('train', visualizations, values, batch_idx, indices=0)
                # image = Image(grid_image, caption="full visualization")
                # visdict[prefix + '_test_' + "visualization"] = image
                if isinstance(self.logger, WandbLogger):
                    self.logger.log_metrics(visdict)#, step=self.global_step)

        if self.logger is not None:
            self.log_dict(losses_and_metrics_to_log, on_step=False, on_epoch=True, sync_dist=True) # log per epoch, # recommended
        # self.log_dict(losses_and_metrics_to_log, on_step=True, on_epoch=False) # log per step
        # self.log_dict(losses_and_metrics_to_log, on_step=True, on_epoch=True) # log per both
        # return losses_and_metrics
        return losses_and_metrics['loss']

    ### STEP ENDS ARE PROBABLY NOT NECESSARY BUT KEEP AN EYE ON THEM IF MULI-GPU TRAINING DOESN'T WORKs
    # def training_step_end(self, batch_parts):
    #     return self._step_end(batch_parts)
    #
    # def validation_step_end(self, batch_parts):
    #     return self._step_end(batch_parts)
    #
    # def _step_end(self, batch_parts):
    #     # gpu_0_prediction = batch_parts.pred[0]['pred']
    #     # gpu_1_prediction = batch_parts.pred[1]['pred']
    #     N = len(batch_parts)
    #     loss_dict = {}
    #     for key in batch_parts[0]:
    #         for i in range(N):
    #             if key not in loss_dict.keys():
    #                 loss_dict[key] = batch_parts[i]
    #             else:
    #                 loss_dict[key] = batch_parts[i]
    #         loss_dict[key] = loss_dict[key] / N
    #     return loss_dict


    def vae_2_str(self, valence=None, arousal=None, affnet_expr=None, expr7=None, prefix=""):
        caption = ""
        if len(prefix) > 0:
            prefix += "_"
        if valence is not None and not np.isnan(valence).any():
            caption += prefix + "valence= %.03f\n" % valence
        if arousal is not None and not np.isnan(arousal).any():
            caption += prefix + "arousal= %.03f\n" % arousal
        if affnet_expr is not None and not np.isnan(affnet_expr).any():
            caption += prefix + "expression= %s \n" % AffectNetExpressions(affnet_expr).name
        if expr7 is not None and not np.isnan(expr7).any():
            caption += prefix +"expression= %s \n" % Expression7(expr7).name
        return caption


    def _create_visualizations_to_log(self, stage, visdict, values, step, indices=None,
                                      dataloader_idx=None, output_dir=None):
        mode_ = str(self.mode.name).lower()
        prefix = self._get_logging_prefix()

        output_dir = output_dir or self.inout_params.full_run_dir

        log_dict = {}
        for key in visdict.keys():
            images = _torch_image2np(visdict[key])
            if images.dtype == np.float32 or images.dtype == np.float64 or images.dtype == np.float16:
                images = np.clip(images, 0, 1)
            if indices is None:
                indices = np.arange(images.shape[0])
            if isinstance(indices, int):
                indices = [indices,]
            if isinstance(indices, str) and indices == 'all':
                image = np.concatenate([images[i] for i in range(images.shape[0])], axis=1)
                savepath = Path(f'{output_dir}/{prefix}_{stage}/{key}/{self.current_epoch:04d}_{step:04d}_all.png')
                # im2log = Image(image, caption=key)
                if isinstance(self.logger, WandbLogger):
                    im2log = _log_wandb_image(savepath, image)
                else:
                    im2log = _log_array_image(savepath, image)
                name = prefix + "_" + stage + "_" + key
                if dataloader_idx is not None:
                    name += "/dataloader_idx_" + str(dataloader_idx)
                log_dict[name] = im2log
            else:
                for i in indices:
                    caption = key + f" batch_index={step}\n"
                    caption += key + f" index_in_batch={i}\n"
                    if self.emonet_loss is not None:
                        if key == 'inputs':
                            if mode_ + "_valence_input" in values.keys():
                                caption += self.vae_2_str(
                                    values[mode_ + "_valence_input"][i].detach().cpu().item(),
                                    values[mode_ + "_arousal_input"][i].detach().cpu().item(),
                                    np.argmax(values[mode_ + "_expression_input"][i].detach().cpu().numpy()),
                                    prefix="emonet") + "\n"
                            if 'va' in values.keys() and mode_ + "valence_gt" in values.keys():
                                # caption += self.vae_2_str(
                                #     values[mode_ + "_valence_gt"][i].detach().cpu().item(),
                                #     values[mode_ + "_arousal_gt"][i].detach().cpu().item(),
                                caption += self.vae_2_str(
                                    values[mode_ + "valence_gt"][i].detach().cpu().item(),
                                    values[mode_ + "arousal_gt"][i].detach().cpu().item(),
                                    prefix="gt") + "\n"
                            if 'expr7' in values.keys() and mode_ + "_expression_gt" in values.keys():
                                caption += "\n" + self.vae_2_str(
                                    expr7=values[mode_ + "_expression_gt"][i].detach().cpu().numpy(),
                                    prefix="gt") + "\n"
                            if 'affectnetexp' in values.keys() and mode_ + "_expression_gt" in values.keys():
                                caption += "\n" + self.vae_2_str(
                                    affnet_expr=values[mode_ + "_expression_gt"][i].detach().cpu().numpy(),
                                    prefix="gt") + "\n"
                        elif 'geometry_detail' in key:
                            if "emo_mlp_valence" in values.keys():
                                caption += self.vae_2_str(
                                    values["emo_mlp_valence"][i].detach().cpu().item(),
                                    values["emo_mlp_arousal"][i].detach().cpu().item(),
                                    prefix="mlp")
                            if 'emo_mlp_expr_classification' in values.keys():
                                caption += "\n" + self.vae_2_str(
                                    affnet_expr=values["emo_mlp_expr_classification"][i].detach().cpu().argmax().numpy(),
                                    prefix="mlp") + "\n"
                        elif key == 'output_images_' + mode_:
                            if mode_ + "_valence_output" in values.keys():
                                caption += self.vae_2_str(values[mode_ + "_valence_output"][i].detach().cpu().item(),
                                                                 values[mode_ + "_arousal_output"][i].detach().cpu().item(),
                                                                 np.argmax(values[mode_ + "_expression_output"][i].detach().cpu().numpy())) + "\n"

                        elif key == 'output_translated_images_' + mode_:
                            if mode_ + "_translated_valence_output" in values.keys():
                                caption += self.vae_2_str(values[mode_ + "_translated_valence_output"][i].detach().cpu().item(),
                                                                 values[mode_ + "_translated_arousal_output"][i].detach().cpu().item(),
                                                                 np.argmax(values[mode_ + "_translated_expression_output"][i].detach().cpu().numpy())) + "\n"


                        # elif key == 'output_images_detail':
                        #     caption += "\n" + self.vae_2_str(values["detail_output_valence"][i].detach().cpu().item(),
                        #                                  values["detail_output_valence"][i].detach().cpu().item(),
                        #                                  np.argmax(values["detail_output_expression"][
                        #                                                i].detach().cpu().numpy()))
                    savepath = Path(f'{output_dir}/{prefix}_{stage}/{key}/{self.current_epoch:04d}_{step:04d}_{i:02d}.png')
                    image = images[i]
                    # im2log = Image(image, caption=caption)
                    if isinstance(self.logger, WandbLogger):
                        im2log = _log_wandb_image(savepath, image, caption)
                    elif self.logger is not None:
                        im2log = _log_array_image(savepath, image, caption)
                    else:
                        im2log = _log_array_image(None, image, caption)
                    name = prefix + "_" + stage + "_" + key
                    if dataloader_idx is not None:
                        name += "/dataloader_idx_" + str(dataloader_idx)
                    log_dict[name] = im2log
        return log_dict

    def _visualization_checkpoint(self, verts, trans_verts, ops, uv_detail_normals, additional, batch_idx, stage, prefix,
                                  save=False):
        batch_size = verts.shape[0]
        visind = np.arange(batch_size)
        shape_images = self.deca.render.render_shape(verts, trans_verts)
        if uv_detail_normals is not None:
            detail_normal_images = F.grid_sample(uv_detail_normals.detach(), ops['grid'].detach(),
                                                 align_corners=False)
            shape_detail_images = self.deca.render.render_shape(verts, trans_verts,
                                                           detail_normal_images=detail_normal_images)
        else:
            shape_detail_images = None

        visdict = {}
        if 'images' in additional.keys():
            visdict['inputs'] = additional['images'][visind]

        if 'images' in additional.keys() and 'lmk' in additional.keys():
            visdict['landmarks_gt'] = util.tensor_vis_landmarks(additional['images'][visind], additional['lmk'][visind])

        if 'images' in additional.keys() and 'predicted_landmarks' in additional.keys():
            visdict['landmarks_predicted'] = util.tensor_vis_landmarks(additional['images'][visind],
                                                                     additional['predicted_landmarks'][visind])

        if 'predicted_images' in additional.keys():
            visdict['output_images_coarse'] = additional['predicted_images'][visind]

        if 'predicted_translated_image' in additional.keys() and additional['predicted_translated_image'] is not None:
            visdict['output_translated_images_coarse'] = additional['predicted_translated_image'][visind]

        visdict['geometry_coarse'] = shape_images[visind]
        if shape_detail_images is not None:
            visdict['geometry_detail'] = shape_detail_images[visind]

        if 'albedo_images' in additional.keys():
            visdict['albedo_images'] = additional['albedo_images'][visind]

        if 'masks' in additional.keys():
            visdict['mask'] = additional['masks'].repeat(1, 3, 1, 1)[visind]
        if 'albedo' in additional.keys():
            visdict['albedo'] = additional['albedo'][visind]

        if 'predicted_detailed_image' in additional.keys() and additional['predicted_detailed_image'] is not None:
            visdict['output_images_detail'] = additional['predicted_detailed_image'][visind]

        if 'predicted_detailed_translated_image' in additional.keys() and additional['predicted_detailed_translated_image'] is not None:
            visdict['output_translated_images_detail'] = additional['predicted_detailed_translated_image'][visind]

        if 'shape_detail_images' in additional.keys():
            visdict['shape_detail_images'] = additional['shape_detail_images'][visind]

        if 'uv_detail_normals' in additional.keys():
            visdict['uv_detail_normals'] = additional['uv_detail_normals'][visind] * 0.5 + 0.5

        if 'uv_texture_patch' in additional.keys():
            visdict['uv_texture_patch'] = additional['uv_texture_patch'][visind]

        if 'uv_texture_gt' in additional.keys():
            visdict['uv_texture_gt'] = additional['uv_texture_gt'][visind]

        if 'translated_uv_texture' in additional.keys() and additional['translated_uv_texture'] is not None:
            visdict['translated_uv_texture'] = additional['translated_uv_texture'][visind]

        if 'uv_vis_mask_patch' in additional.keys():
            visdict['uv_vis_mask_patch'] = additional['uv_vis_mask_patch'][visind]

        if save:
            savepath = f'{self.inout_params.full_run_dir}/{prefix}_{stage}/combined/{self.current_epoch:04d}_{batch_idx:04d}.png'
            Path(savepath).parent.mkdir(exist_ok=True, parents=True)
            visualization_image = self.deca.visualize(visdict, savepath)
            return visdict, visualization_image[..., [2, 1, 0]]
        else:
            visualization_image = None
            return visdict, None

    def _get_trainable_parameters(self):
        trainable_params = []
        if self.mode == DecaMode.COARSE:
            trainable_params += self.deca._get_coarse_trainable_parameters()
        elif self.mode == DecaMode.DETAIL:
            trainable_params += self.deca._get_detail_trainable_parameters()
        else:
            raise ValueError(f"Invalid deca mode: {self.mode}")

        if self.emotion_mlp is not None:
            trainable_params += list(self.emotion_mlp.parameters())

        if self.emonet_loss is not None:
            trainable_params += self.emonet_loss._get_trainable_params()

        return trainable_params


    def configure_optimizers(self):
        # optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        print("Configuring optimizer")

        trainable_params = self._get_trainable_parameters()

        if self.learning_params.optimizer == 'Adam':
            self.deca.opt = torch.optim.Adam(
                trainable_params,
                lr=self.learning_params.learning_rate,
                amsgrad=False)
        elif self.config.learning.optimizer == 'AdaBound':
            opt = adabound.AdaBound(
                trainable_params,
                lr=self.config.learning.learning_rate,
                final_lr=self.config.learning.final_learning_rate
            )
        elif self.learning_params.optimizer == 'SGD':
            self.deca.opt = torch.optim.SGD(
                trainable_params,
                lr=self.learning_params.learning_rate)
        else:
            raise ValueError(f"Unsupported optimizer: '{self.learning_params.optimizer}'")

        optimizers = [self.deca.opt]
        schedulers = []
        if 'learning_rate_decay' in self.learning_params.keys():
            scheduler = torch.optim.lr_scheduler.ExponentialLR(self.deca.opt, gamma=self.learning_params.learning_rate_decay)
            schedulers += [scheduler]
        if len(schedulers) == 0:
            return self.deca.opt

        return optimizers, schedulers


class DECA(torch.nn.Module):

    def __init__(self, config):
        super().__init__()
        self.perceptual_loss = None
        self.id_loss = None
        self.vgg_loss = None
        self._reconfigure(config)
        self._reinitialize()

    def _reconfigure(self, config):
        self.config = config
        self.n_param = config.n_shape + config.n_tex + config.n_exp + config.n_pose + config.n_cam + config.n_light
        self.n_detail = config.n_detail
        self.n_cond = 3 + config.n_exp
        self.mode = DecaMode[str(config.mode).upper()]
        self._init_deep_losses()
        self._setup_neural_rendering()

    def _reinitialize(self):
        self._create_model()
        self._setup_renderer()
        self._init_deep_losses()
        self.face_attr_mask = util.load_local_mask(image_size=self.config.uv_size, mode='bbx')

    def _init_deep_losses(self):
        if 'mrfwr' not in self.config.keys() or self.config.mrfwr == 0:
            self.perceptual_loss = None
        else:
            if self.perceptual_loss is None:
                self.perceptual_loss = lossfunc.IDMRFLoss().eval()
                self.perceptual_loss.requires_grad_(False)  # TODO, move this to the constructor

        if 'idw' not in self.config.keys() or self.config.idw == 0:
            self.id_loss = None
        else:
            if self.id_loss is None:
                self.id_loss = lossfunc.VGGFace2Loss(self.config.pretrained_vgg_face_path).eval()
                self.id_loss.requires_grad_(False) # TODO, move this to the constructor

        if 'vggw' not in self.config.keys() or self.config.vggw == 0:
            self.vgg_loss = None
        else:
            if self.vgg_loss is None:
                vgg_loss_batch_norm = 'vgg_loss_batch_norm' in self.config.keys() and self.config.vgg_loss_batch_norm
                self.vgg_loss = VGG19Loss(dict(zip(self.config.vgg_loss_layers, self.config.lambda_vgg_layers)), batch_norm=vgg_loss_batch_norm).eval()
                self.vgg_loss.requires_grad_(False) # TODO, move this to the constructor

    def _setup_renderer(self):
        self.render = SRenderY(self.config.image_size, obj_filename=self.config.topology_path,
                               uv_size=self.config.uv_size)  # .to(self.device)
        # face mask for rendering details
        mask = imread(self.config.face_mask_path).astype(np.float32) / 255.
        mask = torch.from_numpy(mask[:, :, 0])[None, None, :, :].contiguous()
        self.uv_face_mask = F.interpolate(mask, [self.config.uv_size, self.config.uv_size])
        mask = imread(self.config.face_eye_mask_path).astype(np.float32) / 255.
        mask = torch.from_numpy(mask[:, :, 0])[None, None, :, :].contiguous()
        # self.uv_face_eye_mask = F.interpolate(mask, [self.config.uv_size, self.config.uv_size])
        uv_face_eye_mask = F.interpolate(mask, [self.config.uv_size, self.config.uv_size])
        self.register_buffer('uv_face_eye_mask', uv_face_eye_mask)

        ## displacement correct
        if os.path.isfile(self.config.fixed_displacement_path):
            fixed_dis = np.load(self.config.fixed_displacement_path)
            # self.fixed_uv_dis = torch.tensor(fixed_dis).float()
            fixed_uv_dis = torch.tensor(fixed_dis).float()
        else:
            fixed_uv_dis = torch.zeros([512, 512]).float()
        self.register_buffer('fixed_uv_dis', fixed_uv_dis)


    def _has_neural_rendering(self):
        return hasattr(self.config, "neural_renderer") and bool(self.config.neural_renderer)

    def _setup_neural_rendering(self):
        if self._has_neural_rendering():
            if self.config.neural_renderer.class_ == "StarGAN":
                from .StarGAN import StarGANWrapper
                print("Creating StarGAN neural renderer")
                self.image_translator = StarGANWrapper(self.config.neural_renderer.cfg, self.config.neural_renderer.stargan_repo)
            else:
                raise ValueError(f"Unsupported neural renderer class '{self.config.neural_renderer.class_}'")

            if self.image_translator.background_mode == "input":
                if self.config.background_from_input not in [True, "input"]:
                    raise NotImplementedError("The background mode of the neural renderer and deca is not synchronized. "
                                              "Background should be inpainted from the input")
            elif self.image_translator.background_mode == "black":
                if self.config.background_from_input not in [False, "black"]:
                    raise NotImplementedError("The background mode of the neural renderer and deca is not synchronized. "
                                              "Background should be black.")
            elif self.image_translator.background_mode == "none":
                if self.config.background_from_input not in ["none"]:
                    raise NotImplementedError("The background mode of the neural renderer and deca is not synchronized. "
                                              "The background should not be handled")
            else:
                raise NotImplementedError(f"Unsupported mode of the neural renderer backroungd: "
                                          f"'{self.image_translator.background_mode}'")


    def _create_model(self):
        # coarse shape
        e_flame_type = 'ResnetEncoder'
        if 'e_flame_type' in self.config.keys():
            e_flame_type = self.config.e_flame_type

        if e_flame_type == 'ResnetEncoder':
            self.E_flame = ResnetEncoder(outsize=self.n_param)
        elif e_flame_type[:4] == 'swin':
            self.E_flame = SwinEncoder(outsize=self.n_param, img_size=self.config.image_size, swin_type=e_flame_type)
        else:
            raise ValueError(f"Invalid 'e_flame_type' = {e_flame_type}")

        self.flame = FLAME(self.config)
        self.flametex = FLAMETex(self.config)
        # detail modeling
        e_detail_type = 'ResnetEncoder'
        if 'e_detail_type' in self.config.keys():
            e_detail_type = self.config.e_detail_type

        if e_detail_type == 'ResnetEncoder':
            self.E_detail = ResnetEncoder(outsize=self.n_detail)
        elif e_flame_type[:4] == 'swin':
            self.E_detail = SwinEncoder(outsize=self.n_detail, img_size=self.config.image_size, swin_type=e_detail_type)
        else:
            raise ValueError(f"Invalid 'e_detail_type'={e_detail_type}")

        self.D_detail = Generator(latent_dim=self.n_detail + self.n_cond, out_channels=1, out_scale=0.01,
                                  sample_mode='bilinear')
        # self._load_old_checkpoint()

    def _get_coarse_trainable_parameters(self):
        print("Add E_flame.parameters() to the optimizer")
        return list(self.E_flame.parameters())

    def _get_detail_trainable_parameters(self):
        trainable_params = []
        if self.config.train_coarse:
            trainable_params += self._get_coarse_trainable_parameters()
            print("Add E_flame.parameters() to the optimizer")
        trainable_params += list(self.E_detail.parameters())
        print("Add E_detail.parameters() to the optimizer")
        trainable_params += list(self.D_detail.parameters())
        print("Add D_detail.parameters() to the optimizer")
        return trainable_params

    def train(self, mode: bool = True):
        if mode:
            if self.mode == DecaMode.COARSE:
                self.E_flame.train()
                # print("Setting E_flame to train")
                self.E_detail.eval()
                # print("Setting E_detail to eval")
                self.D_detail.eval()
                # print("Setting D_detail to eval")
            elif self.mode == DecaMode.DETAIL:
                if self.config.train_coarse:
                    # print("Setting E_flame to train")
                    self.E_flame.train()
                else:
                    # print("Setting E_flame to eval")
                    self.E_flame.eval()
                self.E_detail.train()
                # print("Setting E_detail to train")
                self.D_detail.train()
                # print("Setting D_detail to train")
            else:
                raise ValueError(f"Invalid mode '{self.mode}'")
        else:
            self.E_flame.eval()
            # print("Setting E_flame to eval")
            self.E_detail.eval()
            # print("Setting E_detail to eval")
            self.D_detail.eval()
            # print("Setting D_detail to eval")

        # these are set to eval no matter what, they're never being trained
        self.flame.eval()
        self.flametex.eval()
        return self


    def _load_old_checkpoint(self):
        if self.config.resume_training:
            model_path = self.config.pretrained_modelpath
            print(f"Loading model state from '{model_path}'")
            checkpoint = torch.load(model_path)
            # model
            util.copy_state_dict(self.E_flame.state_dict(), checkpoint['E_flame'])
            # util.copy_state_dict(self.opt.state_dict(), checkpoint['opt']) # deprecate
            # detail model
            if 'E_detail' in checkpoint.keys():
                util.copy_state_dict(self.E_detail.state_dict(), checkpoint['E_detail'])
                util.copy_state_dict(self.D_detail.state_dict(), checkpoint['D_detail'])
            # training state
            self.start_epoch = 0  # checkpoint['epoch']
            self.start_iter = 0  # checkpoint['iter']
        else:
            print('Start training from scratch')
            self.start_epoch = 0
            self.start_iter = 0

    def _encode_flame(self, images):
        return self.E_flame(images)

    def decompose_code(self, code):
        '''
        config.n_shape + config.n_tex + config.n_exp + config.n_pose + config.n_cam + config.n_light
        '''
        code_list = []
        num_list = [self.config.n_shape, self.config.n_tex, self.config.n_exp, self.config.n_pose, self.config.n_cam,
                    self.config.n_light]
        start = 0
        for i in range(len(num_list)):
            code_list.append(code[:, start:start + num_list[i]])
            start = start + num_list[i]
        # shapecode, texcode, expcode, posecode, cam, lightcode = code_list
        code_list[-1] = code_list[-1].reshape(code.shape[0], 9, 3)
        return code_list

    def displacement2normal(self, uv_z, coarse_verts, coarse_normals):
        batch_size = uv_z.shape[0]
        uv_coarse_vertices = self.render.world2uv(coarse_verts).detach()
        uv_coarse_normals = self.render.world2uv(coarse_normals).detach()

        uv_z = uv_z * self.uv_face_eye_mask

        # detail vertices = coarse vertice + predicted displacement*normals + fixed displacement*normals
        uv_detail_vertices = uv_coarse_vertices + \
                             uv_z * uv_coarse_normals + \
                             self.fixed_uv_dis[None, None, :,:] * uv_coarse_normals.detach()

        dense_vertices = uv_detail_vertices.permute(0, 2, 3, 1).reshape([batch_size, -1, 3])
        uv_detail_normals = util.vertex_normals(dense_vertices, self.render.dense_faces.expand(batch_size, -1, -1))
        uv_detail_normals = uv_detail_normals.reshape(
            [batch_size, uv_coarse_vertices.shape[2], uv_coarse_vertices.shape[3], 3]).permute(0, 3, 1, 2)
        # uv_detail_normals = uv_detail_normals*self.uv_face_eye_mask + uv_coarse_normals*(1-self.uv_face_eye_mask)
        # uv_detail_normals = util.gaussian_blur(uv_detail_normals)
        return uv_detail_normals, uv_coarse_vertices

    def visualize(self, visdict, savepath):
        grids = {}
        for key in visdict:
            # print(key)
            if visdict[key] is None:
                continue
            grids[key] = torchvision.utils.make_grid(
                F.interpolate(visdict[key], [self.config.image_size, self.config.image_size])).detach().cpu()
        grid = torch.cat(list(grids.values()), 1)
        grid_image = (grid.numpy().transpose(1, 2, 0).copy() * 255)[:, :, [2, 1, 0]]
        grid_image = np.minimum(np.maximum(grid_image, 0), 255).astype(np.uint8)
        cv2.imwrite(savepath, grid_image)
        return grid_image

    def create_mesh(self, opdict, dense_template):
        '''
        vertices: [nv, 3], tensor
        texture: [3, h, w], tensor
        '''
        i = 0
        vertices = opdict['verts'][i].cpu().numpy()
        faces = self.render.faces[0].cpu().numpy()
        texture = util.tensor2image(opdict['uv_texture_gt'][i])
        uvcoords = self.render.raw_uvcoords[0].cpu().numpy()
        uvfaces = self.render.uvfaces[0].cpu().numpy()
        # save coarse mesh, with texture and normal map
        normal_map = util.tensor2image(opdict['uv_detail_normals'][i]*0.5 + 0.5)

        # upsample mesh, save detailed mesh
        texture = texture[:,:,[2,1,0]]
        normals = opdict['normals'][i].cpu().numpy()
        displacement_map = opdict['displacement_map'][i].detach().cpu().numpy().squeeze()
        dense_vertices, dense_colors, dense_faces = util.upsample_mesh(vertices, normals, faces,
                                                                       displacement_map, texture, dense_template)
        return vertices, faces, texture, uvcoords, uvfaces, normal_map, dense_vertices, dense_faces, dense_colors


    def save_obj(self, filename, opdict, dense_template, mode ='detail'):
        if mode not in ['coarse', 'detail', 'both']:
            raise ValueError(f"Invalid mode '{mode}. Expected modes are: 'coarse', 'detail', 'both'")

        vertices, faces, texture, uvcoords, uvfaces, normal_map, dense_vertices, dense_faces, dense_colors \
            = self.create_mesh(opdict, dense_template)

        if mode == 'both':
            if isinstance(filename, list):
                filename_coarse = filename[0]
                filename_detail = filename[1]
            else:
                filename_coarse = filename
                filename_detail = filename.replace('.obj', '_detail.obj')
        elif mode == 'coarse':
            filename_coarse = filename
        else:
            filename_detail = filename

        if mode in ['coarse', 'both']:
            util.write_obj(str(filename_coarse), vertices, faces,
                            texture=texture,
                            uvcoords=uvcoords,
                            uvfaces=uvfaces,
                            normal_map=normal_map)

        if mode in ['detail', 'both']:
            util.write_obj(str(filename_detail),
                            dense_vertices,
                            dense_faces,
                            colors = dense_colors,
                            inverse_face_order=True)


from gdl.models.EmoNetRegressor import EmoNetRegressor, EmonetRegressorStatic

class ExpDECA(DECA):

    def _create_model(self):
        super()._create_model()
        # E_flame should be fixed for expression DECA
        self.E_flame.requires_grad_(False)
        if self.config.expression_backbone == 'deca_parallel':
            ## Attach a parallel flow of FCs onto deca coarse backbone
            self.E_expression = SecondHeadResnet(self.E_flame, self.n_exp_param, 'same')
        elif self.config.expression_backbone == 'deca_clone':
            #TODO this will only work for Resnet. Make this work for the other backbones (Swin) as well.
            self.E_expression = ResnetEncoder(self.n_exp_param)
            # clone parameters of the ResNet
            self.E_expression.encoder.load_state_dict(self.E_flame.encoder.state_dict())
        elif self.config.expression_backbone == 'emonet_trainable':
            self.E_expression = EmoNetRegressor(self.n_exp_param)
        elif self.config.expression_backbone == 'emonet_static':
            self.E_expression = EmonetRegressorStatic(self.n_exp_param)
        else:
            raise ValueError(f"Invalid expression backbone: '{self.config.expression_backbone}'")

    def _get_coarse_trainable_parameters(self):
        print("Add E_expression.parameters() to the optimizer")
        return list(self.E_expression.parameters())

    def _reconfigure(self, config):
        super()._reconfigure(config)
        self.n_exp_param = self.config.n_exp

        if self.config.exp_deca_global_pose and self.config.exp_deca_jaw_pose:
            self.n_exp_param += self.config.n_pose
        elif self.config.exp_deca_global_pose or self.config.exp_deca_jaw_pose:
            self.n_exp_param += 3

    def _encode_flame(self, images):
        if self.config.expression_backbone == 'deca_parallel':
            #SecondHeadResnet does the forward pass for shape and expression at the same time
            return self.E_expression(images)
        # other regressors have to do a separate pass over the image
        deca_code = super()._encode_flame(images)
        exp_deca_code = self.E_expression(images)
        return deca_code, exp_deca_code

    def decompose_code(self, code):
        deca_code = code[0]
        expdeca_code = code[1]

        deca_code_list = super().decompose_code(deca_code)
        # shapecode, texcode, expcode, posecode, cam, lightcode = deca_code_list
        exp_idx = 2
        pose_idx = 3

        if self.config.exp_deca_global_pose and self.config.exp_deca_jaw_pose:
            exp_code = expdeca_code[:, :self.config.n_exp]
            pose_code = expdeca_code[:, self.config.n_exp:]
            deca_code_list[exp_idx] = exp_code
            deca_code_list[pose_idx] = pose_code
        elif self.config.exp_deca_global_pose:
            # global pose from ExpDeca, jaw pose from DECA
            pose_code_exp_deca = expdeca_code[:, self.config.n_exp:]
            pose_code_deca = deca_code_list[pose_idx]
            deca_code_list[pose_idx] = torch.cat([pose_code_exp_deca, pose_code_deca[:,3:]], dim=1)
        elif self.config.exp_deca_jaw_pose:
            # global pose from DECA, jaw pose from ExpDeca
            pose_code_exp_deca = expdeca_code[:, self.config.n_exp:]
            pose_code_deca = deca_code_list[pose_idx]
            deca_code_list[pose_idx] = torch.cat([pose_code_deca[:, :3], pose_code_exp_deca], dim=1)
        else:
            exp_code = expdeca_code
            deca_code_list[exp_idx] = exp_code

        return deca_code_list

    def train(self, mode: bool = True):
        super().train(mode)

        # for expression deca, we are not training teh resnet feature extractor plus the identity/light/texture regressor
        self.E_flame.eval()

        if mode:
            if self.mode == DecaMode.COARSE:
                self.E_expression.train()
                # print("Setting E_expression to train")
                self.E_detail.eval()
                # print("Setting E_detail to eval")
                self.D_detail.eval()
                # print("Setting D_detail to eval")
            elif self.mode == DecaMode.DETAIL:
                if self.config.train_coarse:
                    # print("Setting E_flame to train")
                    self.E_expression.train()
                else:
                    # print("Setting E_flame to eval")
                    self.E_expression.eval()
                self.E_detail.train()
                # print("Setting E_detail to train")
                self.D_detail.train()
            else:
                raise ValueError(f"Invalid mode '{self.mode}'")
        else:
            self.E_expression.eval()
            self.E_detail.eval()
            self.D_detail.eval()
        return self


def instantiate_deca(cfg, stage, prefix, checkpoint=None, checkpoint_kwargs=None):
    if checkpoint is None:
        deca = DecaModule(cfg.model, cfg.learning, cfg.inout, prefix)
        if cfg.model.resume_training:
            print("[WARNING] Loading DECA checkpoint pretrained by the old code")
            deca.deca._load_old_checkpoint()
    else:
        checkpoint_kwargs = checkpoint_kwargs or {}
        deca = DecaModule.load_from_checkpoint(checkpoint_path=checkpoint, strict=False, **checkpoint_kwargs)
        if stage == 'train':
            mode = True
        else:
            mode = False
        deca.reconfigure(cfg.model, cfg.inout, prefix, downgrade_ok=True, train=mode)
    return deca
