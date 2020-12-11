import os

import nibabel as nib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from skimage.measure import label, regionprops
from skimage.morphology import remove_small_objects
from tqdm import tqdm

from dataset.fracnet_dataset import FracNetInferenceDataset
from dataset import transforms as tsfm
from model.unet import UNet


prob_thresh = 0.1
bone_thresh = 300
size_thresh = 100


def _predict_single_image(model, dataloader):
    pred = np.zeros(dataloader.dataset.image.shape)
    crop_size = dataloader.dataset.crop_size
    with torch.no_grad():
        for _, sample in enumerate(dataloader):
            images, centers = sample
            images = images.cuda()
            output = model(images).sigmoid().cpu().numpy().squeeze(axis=1)

            for i in range(len(centers)):
                center_x, center_y, center_z = centers[i]
                cur_pred_patch = pred[
                    center_x - crop_size // 2:center_x + crop_size // 2,
                    center_y - crop_size // 2:center_y + crop_size // 2,
                    center_z - crop_size // 2:center_z + crop_size // 2
                ]
                pred[
                    center_x - crop_size // 2:center_x + crop_size // 2,
                    center_y - crop_size // 2:center_y + crop_size // 2,
                    center_z - crop_size // 2:center_z + crop_size // 2
                ] = np.amax((output[i], cur_pred_patch), axis=0)

    return pred


def _remove_low_probs(pred, prob_thresh):
    pred = np.where(pred > prob_thresh, pred, 0)

    return pred


def _remove_spine_fp(pred, image, bone_thresh):
    center_x = image.shape[0] // 2
    bone = image > bone_thresh
    bone_x_sum = bone.sum(axis=(1, 2))
    bone_x_regions = label(bone_x_sum > bone_x_sum.mean())

    spine_x_range = (256 - 50, 256 + 50)
    for i in range(bone_x_regions.max()):
        cur_region = bone_x_regions == i
        if cur_region[center_x]:
            spine_coords = np.argwhere(cur_region > 0)
            spine_x_range = spine_coords.min(), spine_coords.max()
            break

    pred[spine_x_range[0]:spine_x_range[1]] = 0

    return pred


def _remove_small_objects(pred, size_thresh):
    pred_bin = pred > 0
    pred_bin = remove_small_objects(pred_bin, size_thresh)
    pred = np.where(pred_bin, pred, 0)

    return pred


def _post_process(pred, image, prob_thresh, bone_thresh, size_thresh):
    # remove connected regions with low confidence
    pred = _remove_low_probs(pred, prob_thresh)

    # remove spine false positives
    pred = _remove_spine_fp(pred, image, bone_thresh)

    # remove small connected regions
    pred = _remove_small_objects(pred, size_thresh)

    return pred


def _make_submission_files(pred, image_id, affine):
    pred_label = label(pred > 0).astype(np.int16)
    pred_regions = regionprops(pred_label, pred)
    pred_index = [0] + [region.label for region in pred_regions]
    pred_proba = [0.0] + [region.mean_intensity for region in pred_regions]
    # placeholder for label class since classifaction isn't included
    pred_label_code = [0] + [1] * int(pred_label.max())
    pred_image = nib.Nifti1Image(pred_label, affine)
    pred_info = pd.DataFrame({
        "public_id": [image_id] * len(pred_index),
        "label_id": pred_index,
        "confidence": pred_proba,
        "label_code": pred_label_code
    })

    return pred_image, pred_info


def predict(args):
    batch_size = 16
    num_workers = 4

    model = UNet(1, 1, n=16)
    model.eval()
    if args.model_path is not None:
        model_weights = torch.load(args.model_path)
        model.load_state_dict(model_weights)
    model = nn.DataParallel(model).cuda()

    transforms = [
        tsfm.Window(-200, 1000),
        tsfm.MinMaxNorm(-200, 1000)
    ]

    image_path_list = sorted([os.path.join(args.image_dir, file)
        for file in os.listdir(args.image_dir) if "nii" in file])
    image_id_list = [os.path.basename(path).split("-")[0]
        for path in image_path_list]

    progress = tqdm(total=len(image_id_list))
    pred_info_list = []
    for image_id, image_path in zip(image_id_list, image_path_list):
        dataset = FracNetInferenceDataset(image_path, transforms=transforms)
        dataloader = FracNetInferenceDataset.get_dataloader(dataset,
            batch_size, num_workers)
        pred_arr = _predict_single_image(model, dataloader)
        pred_arr = _post_process(pred_arr, dataset.image, prob_thresh,
            bone_thresh, size_thresh)
        pred_image, pred_info = _make_submission_files(pred_arr, image_id,
            dataset.image_affine)
        pred_info_list.append(pred_info)
        pred_path = os.path.join(args.pred_dir, f"{image_id}_pred.nii.gz")
        nib.save(pred_image, pred_path)

        progress.update()

    pred_info = pd.concat(pred_info_list, ignore_index=True)
    pred_info.to_csv(os.path.join(args.pred_dir, "pred_info.csv"),
        index=False)


if __name__ == "__main__":
    import argparse


    parser = argparse.ArgumentParser()
    parser.add_argument("--image_dir", required=True,
        help="The image nii directory.")
    parser.add_argument("--pred_dir", required=True,
        help="The directory for saving predictions.")
    parser.add_argument("--model_path", default=None,
        help="The PyTorch model weight path.")
    args = parser.parse_args()
    predict(args)
