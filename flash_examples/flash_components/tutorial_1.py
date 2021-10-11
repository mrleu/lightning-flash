# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from contextlib import suppress
from typing import Any, Callable, Dict, List, Optional

import torchvision.transforms as T
from PIL import Image
from pytorch_lightning.trainer.states import RunningStage
from pytorch_lightning.utilities.enums import LightningEnum
from torch.utils.data._utils.collate import default_collate

from flash import FlashDataset, PreTransform, PreTransformPlacement
from flash.core.data.data_module import DataModule
from flash.core.data.data_source import DefaultDataKeys
from flash.core.data.preprocess_transform import PRE_TRANSFORM_TYPE
from flash.core.data.transforms import ApplyToKeys
from flash.core.registry import FlashRegistry

#############################################################################################
#                    Use Case: Load Data from multiple folders                              #
# Imagine you have some images and they are already sorted by classes in their own folder.  #
# You would like to create your own loading mechanism that people can re-use                #
# Your loader would take a list of individual class folder and load the images from them    #
# The folder paths are independent and when loading the order of folder.                    #
# would determine the classification label.                                                 #
# Note: This is simple enough to show you the flexibility of the Flash API.                 #
#############################################################################################


#############################################################################################
#            Step 1 / 5: Create an enum to describe your new loading mechanism              #
#############################################################################################


class CustomDataTransform(LightningEnum):

    BASE = "base"
    RANDOM_ROTATION = "random_rotation"


class CustomDataFormat(LightningEnum):

    MULTIPLE_FOLDERS = "multiple_folders"


#############################################################################################
#                         Step 2 / 5: Implement a FlashDataset                              #
#                                                                                           #
# A `FlashDataset` is a state-aware (c.f training, validating, testing and predicting)      #
# dataset.                                                                                  #
# and with specialized hooks (c.f load_data, load_sample) for each of those stages.         #
# The hook resolution for the function is done in the following way.                        #
# If {state}_load_data is implemented then it would be used exclusively for that stage.     #
# Otherwise, it would use the load_data function.                                           #
#                                                                                           #
#############################################################################################

FOLDER_PATH = "./data/hymenoptera_data/train"
TRAIN_FOLDERS = [os.path.join(FOLDER_PATH, "ants"), os.path.join(FOLDER_PATH, "bees")]
VAL_FOLDERS = [os.path.join(FOLDER_PATH, "ants"), os.path.join(FOLDER_PATH, "bees")]
PREDICT_FOLDER = os.path.join(FOLDER_PATH, "ants")


class MultipleFoldersImageDataset(FlashDataset):

    transform_registry = FlashRegistry("image_classification_transform")

    def load_data(self, folders: List[str]) -> List[Dict[DefaultDataKeys, Any]]:
        if self.training:
            self.num_classes = len(folders)
        return [
            {DefaultDataKeys.INPUT: os.path.join(folder, p), DefaultDataKeys.TARGET: class_idx}
            for class_idx, folder in enumerate(folders)
            for p in os.listdir(folder)
        ]

    def load_sample(self, sample: Dict[DefaultDataKeys, Any]) -> Dict[DefaultDataKeys, Any]:
        sample[DefaultDataKeys.INPUT] = image = Image.open(sample[DefaultDataKeys.INPUT])
        sample[DefaultDataKeys.METADATA] = image.size
        return sample

    def predict_load_data(self, predict_folder: str) -> List[Dict[DefaultDataKeys, Any]]:
        assert os.path.isdir(predict_folder)
        return [{DefaultDataKeys.INPUT: os.path.join(predict_folder, p)} for p in os.listdir(predict_folder)]


train_dataset = MultipleFoldersImageDataset.from_data(TRAIN_FOLDERS, running_stage=RunningStage.TRAINING)
val_dataset = MultipleFoldersImageDataset.from_data(VAL_FOLDERS, running_stage=RunningStage.VALIDATING)
predict_dataset = MultipleFoldersImageDataset.from_data(PREDICT_FOLDER, running_stage=RunningStage.PREDICTING)


#############################################################################################
#                   Step 3 / 5: [optional] Implement a PreTransform                       #
#                                                                                           #
# A `PreTransform` is a state-aware (c.f training, validating, testing and predicting)    #
# transform. You would have to implement a `configure_transforms` hook with your transform  #
#                                                                                           #
#############################################################################################


class ImageBaseTransform(PreTransform):
    def configure_transforms(self, image_size: int = 224) -> Dict[PreTransformPlacement, Callable]:
        return {
            PreTransformPlacement.PER_SAMPLE_TRANSFORM: ApplyToKeys(
                DefaultDataKeys.INPUT, T.Compose([T.Resize((image_size, image_size)), T.ToTensor()])
            ),
            PreTransformPlacement.COLLATE: default_collate,
        }


class ImageRandomRotationTransform(ImageBaseTransform):
    def configure_transforms(self, image_size: int = 224, rotation: float = 0) -> Dict[PreTransformPlacement, Callable]:
        transforms = [T.Resize((image_size, image_size)), T.ToTensor()]
        if self.training:
            transforms += [T.RandomRotation(rotation)]
        return {
            PreTransformPlacement.PER_SAMPLE_TRANSFORM: ApplyToKeys(DefaultDataKeys.INPUT, T.Compose(transforms)),
            PreTransformPlacement.COLLATE: default_collate,
        }


# Register your transform within the Flash Dataset registry
# Note: Registries can be shared by multiple dataset.
MultipleFoldersImageDataset.register_transform(CustomDataTransform.BASE, ImageBaseTransform)
MultipleFoldersImageDataset.register_transform(CustomDataTransform.RANDOM_ROTATION, ImageRandomRotationTransform)

train_dataset = MultipleFoldersImageDataset.from_data(
    TRAIN_FOLDERS,
    running_stage=RunningStage.TRAINING,
    transform=(CustomDataTransform.RANDOM_ROTATION, {"rotation": 45}),
)

print(train_dataset.transform)
# Out:
# ImageClassificationRandomRotationTransform(
#    running_stage=train,
#    transform={
#        PreTransformPlacement.PER_SAMPLE_TRANSFORM: ApplyToKeys(
#           keys="input",
#           transform=Compose(
#                ToTensor()
#                RandomRotation(degrees=[-45.0, 45.0], interpolation=nearest, expand=False, fill=0)),
#        PreTransformPlacement.COLLATE: default_collate,
#    },
# )


validation_dataset = MultipleFoldersImageDataset.from_data(
    VAL_FOLDERS, running_stage=RunningStage.VALIDATING, transform=CustomDataTransform.BASE
)
print(validation_dataset.transform)
# Out:
# ImageClassificationRandomRotationTransform(
#    running_stage=validate,
#    transform={
#        PreTransformPlacement.PER_SAMPLE_TRANSFORM: ApplyToKeys(keys="input", transform=ToTensor()),
#        PreTransformPlacement.COLLATE: default_collate,
#    },
# )

print(train_dataset[0])
# Out:
# {
#   <DefaultDataKeys.INPUT: 'input'>: tensor([...]),
#   <DefaultDataKeys.TARGET: 'target'>: 0,
#   <DefaultDataKeys.METADATA: 'metadata'>: (500, 375)
# }

#############################################################################################
#                           Step 4 / 5: Create a DataModule                                 #
#                                                                                           #
# The `ImageClassificationDataModule` class is a collection of FlashDataset
# and its responsability is to create the dataloaders.
#                                                                                           #
#############################################################################################


datamodule = DataModule(train_dataset=train_dataset)


class ImageClassificationDataModule(DataModule):

    flash_datasets_registry = FlashRegistry("image_classification_loader")

    @classmethod
    def from_multiple_folders(
        cls,
        train_folders: Optional[List[str]] = None,
        val_folders: Optional[List[str]] = None,
        test_folders: Optional[List[str]] = None,
        predict_folder: Optional[str] = None,
        train_transform: Optional[PRE_TRANSFORM_TYPE] = None,
        val_transform: Optional[PRE_TRANSFORM_TYPE] = None,
        test_transform: Optional[PRE_TRANSFORM_TYPE] = None,
        predict_transform: Optional[PRE_TRANSFORM_TYPE] = None,
        **data_module_kwargs: Any,
    ) -> "ImageClassificationDataModule":

        datasets = cls.create_flash_datasets(
            CustomDataFormat.MULTIPLE_FOLDERS,
            train_folders,
            val_folders,
            test_folders,
            predict_folder,
            train_transform,
            val_transform,
            test_transform,
            predict_transform,
        )

        return cls(*datasets, **data_module_kwargs)


ImageClassificationDataModule.register_flash_dataset(CustomDataFormat.MULTIPLE_FOLDERS, MultipleFoldersImageDataset)


#############################################################################################
#                         Step 5 / 5: Finally, create the loader                            #
#############################################################################################

datamodule = ImageClassificationDataModule.from_multiple_folders(
    train_folders=TRAIN_FOLDERS,
    val_folders=VAL_FOLDERS,
    predict_folder=PREDICT_FOLDER,
    train_transform=CustomDataTransform.RANDOM_ROTATION,
)

assert isinstance(datamodule.train_dataset, FlashDataset)
assert isinstance(datamodule.predict_dataset, FlashDataset)

# The ``num_classes`` value was set line 76.
assert datamodule.train_dataset.num_classes == 2

# The ``num_classes`` value was set only for training as `self.training` was used,
# so it doesn't exist for the predict_dataset
with suppress(AttributeError):
    datamodule.val_dataset.num_classes

# As test_data weren't provided, the test dataset is None.
assert not datamodule.test_dataset


print(datamodule.train_dataset[0])
# out:
# {
#   <DefaultDataKeys.INPUT: 'input'>: 'data/hymenoptera_data/train/ants/957233405_25c1d1187b.jpg',
#   <DefaultDataKeys.TARGET: 'target'>: 0
#   <DefaultDataKeys.METADATA: 'metadata'>: (...)
# }

assert isinstance(datamodule.predict_dataset, FlashDataset)
print(datamodule.predict_dataset[0])
# out:
# {
#   {<DefaultDataKeys.INPUT: 'input'>: 'data/hymenoptera_data/train/ants/957233405_25c1d1187b.jpg'}
# }


batch = next(iter(datamodule.train_dataloader()))
# Out:
# {
#   <DefaultDataKeys.INPUT: 'input'>: tensor([...]),
#   <DefaultDataKeys.TARGET: 'target'>: tensor([...]),
#   <DefaultDataKeys.METADATA: 'metadata'>: [(...), (...), ...],
# }
print(batch)
