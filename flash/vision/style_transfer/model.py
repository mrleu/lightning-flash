from typing import Any, Dict, Mapping, NoReturn, Optional, Type, Union

import torch
from pystiche import enc, loss, ops
from pystiche.image import read_image
from torch import nn
from torch.nn.functional import interpolate
from torch.optim.lr_scheduler import _LRScheduler

from flash.core import Task
from flash.data.data_source import DefaultDataKeys
from flash.data.process import Serializer

from ._utils import raise_not_supported

__all__ = ["StyleTransfer"]


class Interpolate(nn.Module):

    def __init__(self, scale_factor: float = 1.0, mode: str = "nearest") -> None:
        super().__init__()
        self.scale_factor = scale_factor
        self.mode = mode

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        return interpolate(input, scale_factor=self.scale_factor, mode=self.mode)

    def extra_repr(self) -> str:
        extras = [f"scale_factor={self.scale_factor}"]
        if self.mode != "nearest":
            extras.append(f"mode={self.mode}")
        return ", ".join(extras)


class Conv(nn.Module):

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        kernel_size: int,
        *,
        stride: int = 1,
        upsample: bool = False,
        norm: bool = True,
        activation: bool = True,
    ):
        super().__init__()
        self.upsample = Interpolate(scale_factor=stride) if upsample else None
        self.pad = nn.ReflectionPad2d(kernel_size // 2)
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size, stride=1 if upsample else stride)
        self.norm = nn.InstanceNorm2d(out_channels, affine=True) if norm else None
        self.activation = nn.ReLU() if activation else None

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        if self.upsample:
            input = self.upsample(input)

        output = self.conv(self.pad(input))

        if self.norm:
            output = self.norm(output)
        if self.activation:
            output = self.activation(output)

        return output


class Residual(nn.Module):

    def __init__(self, channels: int) -> None:
        super().__init__()
        self.conv1 = Conv(channels, channels, kernel_size=3)
        self.conv2 = Conv(channels, channels, kernel_size=3, activation=False)

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        output = self.conv2(self.conv1(input))
        return output + input


class FloatToUint8Range(nn.Module):

    def forward(self, input):
        return input * 255.0


class Uint8ToFloatRange(nn.Module):

    def forward(self, input):
        return input / 255.0


class Transformer(nn.Module):

    def __init__(self) -> None:
        super().__init__()
        self.encoder = nn.Sequential(
            Conv(3, 32, kernel_size=9),
            Conv(32, 64, kernel_size=3, stride=2),
            Conv(64, 128, kernel_size=3, stride=2),
            Residual(128),
            Residual(128),
            Residual(128),
            Residual(128),
            Residual(128),
        )
        self.decoder = nn.Sequential(
            Conv(128, 64, kernel_size=3, stride=2, upsample=True),
            Conv(64, 32, kernel_size=3, stride=2, upsample=True),
            Conv(32, 3, kernel_size=9, norm=False, activation=False),
        )

        self.preprocessor = FloatToUint8Range()
        self.postprocessor = Uint8ToFloatRange()

    def forward(self, input):
        input = self.preprocessor(input)
        output = self.decoder(self.encoder(input))
        return self.postprocessor(output)


class StyleTransfer(Task):

    def __init__(
        self,
        style_image: Union[str, torch.Tensor],
        model: Optional[nn.Module] = None,
        multi_layer_encoder: Optional[enc.MultiLayerEncoder] = None,
        content_loss: Optional[Union[ops.ComparisonOperator, ops.OperatorContainer]] = None,
        style_loss: Optional[Union[ops.ComparisonOperator, ops.OperatorContainer]] = None,
        optimizer: Union[Type[torch.optim.Optimizer], torch.optim.Optimizer] = torch.optim.Adam,
        optimizer_kwargs: Optional[Dict[str, Any]] = None,
        scheduler: Optional[Union[Type[_LRScheduler], str, _LRScheduler]] = None,
        scheduler_kwargs: Optional[Dict[str, Any]] = None,
        learning_rate: float = 1e-3,
        serializer: Optional[Union[Serializer, Mapping[str, Serializer]]] = None,
    ):
        self.save_hyperparameters(ignore="style_image")

        if isinstance(style_image, str):
            style_image = read_image(style_image)

        if model is None:
            # TODO: import this from pystiche
            model = Transformer()

        if multi_layer_encoder is None:
            multi_layer_encoder = self.default_multi_layer_encoder()

        if content_loss is None:
            content_loss = self.default_content_loss(multi_layer_encoder)

        if style_loss is None:
            style_loss = self.default_style_loss(multi_layer_encoder)

        perceptual_loss = loss.PerceptualLoss(content_loss, style_loss)

        super().__init__(
            model=model,
            loss_fn=perceptual_loss,
            optimizer=optimizer,
            optimizer_kwargs=optimizer_kwargs,
            scheduler=scheduler,
            scheduler_kwargs=scheduler_kwargs,
            learning_rate=learning_rate,
            serializer=serializer,
        )

        self.perceptual_loss = perceptual_loss
        self.perceptual_loss.set_style_image(style_image)

    def default_multi_layer_encoder(self) -> enc.MultiLayerEncoder:
        return enc.vgg16_multi_layer_encoder()

    def default_content_loss(
        self, multi_layer_encoder: Optional[enc.MultiLayerEncoder] = None
    ) -> ops.FeatureReconstructionOperator:
        if multi_layer_encoder is None:
            multi_layer_encoder = self.default_multi_layer_encoder()
        content_layer = "relu2_2"
        content_encoder = multi_layer_encoder.extract_encoder(content_layer)
        content_weight = 1e5
        return ops.FeatureReconstructionOperator(content_encoder, score_weight=content_weight)

    def default_style_loss(
        self, multi_layer_encoder: Optional[enc.MultiLayerEncoder] = None
    ) -> ops.MultiLayerEncodingOperator:

        class GramOperator(ops.GramOperator):

            def enc_to_repr(self, enc: torch.Tensor) -> torch.Tensor:
                repr = super().enc_to_repr(enc)
                num_channels = repr.size()[1]
                return repr / num_channels

        if multi_layer_encoder is None:
            multi_layer_encoder = self.default_multi_layer_encoder()

        style_layers = ("relu1_2", "relu2_2", "relu3_3", "relu4_3")
        style_weight = 1e10
        return ops.MultiLayerEncodingOperator(
            multi_layer_encoder,
            style_layers,
            lambda encoder, layer_weight: GramOperator(encoder, score_weight=layer_weight),
            layer_weights="sum",
            score_weight=style_weight,
        )

    def step(self, batch: Any, batch_idx: int) -> Any:
        input_image = batch
        self.perceptual_loss.set_content_image(input_image)

        output_image = self(batch)
        loss = self.perceptual_loss(output_image).total()

        logs = dict(perceptual_loss=loss)
        return dict(loss=loss, logs=logs)

    def training_step(self, batch: Any, batch_idx: int) -> Any:
        return super().training_step(batch[DefaultDataKeys.INPUT], batch_idx)

    def validation_step(self, batch: Any, batch_idx: int) -> NoReturn:
        raise_not_supported("validation")

    def test_step(self, batch: Any, batch_idx: int) -> NoReturn:
        raise_not_supported("test")