#!/usr/bin/env python3
"""
GENESIS — SAM2 + LaMa glove removal pipeline

Usage:
  python3 genesis_remove_glove.py --image path/to/frame.png --output result.png

SAM2 segments the glove interactively (click a point on the glove).
LaMa inpaints the masked region to reconstruct the hand underneath.

Requirements:
  source /home/digity/sam-lama-venv/bin/activate
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
import torch

# ── Args ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--image",  required=True, help="Input frame (with glove)")
parser.add_argument("--output", default="genesis_result.png", help="Output path")
parser.add_argument("--point",  nargs=2, type=int, default=None,
                    help="X Y pixel on the glove (skips interactive click)")
args = parser.parse_args()

image_path = Path(args.image)
if not image_path.exists():
    print(f"[error] Image not found: {image_path}")
    sys.exit(1)

# ── Load image ────────────────────────────────────────────────────────────────
print(f"[1/4] Loading image: {image_path}")
image_bgr = cv2.imread(str(image_path))
image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
H, W = image_rgb.shape[:2]
print(f"      Size: {W}×{H}")

# ── Interactive point selection ───────────────────────────────────────────────
clicked_point = None

if args.point:
    clicked_point = args.point
    print(f"[2/4] Using provided point: {clicked_point}")
else:
    print("[2/4] Click on the GLOVE in the window, then press any key.")
    display = image_bgr.copy()
    cv2.putText(display, "Click on the glove, then press any key",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

    def on_mouse(event, x, y, flags, param):
        global clicked_point
        if event == cv2.EVENT_LBUTTONDOWN:
            clicked_point = [x, y]
            cv2.circle(display, (x, y), 8, (0, 0, 255), -1)
            cv2.imshow("Select glove point", display)

    cv2.namedWindow("Select glove point")
    cv2.setMouseCallback("Select glove point", on_mouse)
    cv2.imshow("Select glove point", display)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

if clicked_point is None:
    print("[error] No point selected.")
    sys.exit(1)

print(f"      Point: {clicked_point}")

# ── SAM2 segmentation ─────────────────────────────────────────────────────────
print("[3/4] Running SAM2 segmentation…")

sys.path.insert(0, "/home/digity/sam2")
from sam2.build_sam import build_sam2
from sam2.sam2_image_predictor import SAM2ImagePredictor

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
SAM2_CHECKPOINT = "/home/digity/sam2/checkpoints/sam2.1_hiera_large.pt"
SAM2_CONFIG     = "configs/sam2.1/sam2.1_hiera_l.yaml"

sam2_model = build_sam2(SAM2_CONFIG, SAM2_CHECKPOINT, device=DEVICE)
predictor  = SAM2ImagePredictor(sam2_model)

predictor.set_image(image_rgb)
masks, scores, _ = predictor.predict(
    point_coords=np.array([clicked_point]),
    point_labels=np.array([1]),   # 1 = foreground
    multimask_output=True,
)

# Pick the mask with the highest score
best_idx  = int(np.argmax(scores))
mask      = masks[best_idx].astype(np.uint8)  # 0/1

# Dilate slightly so LaMa covers edges
kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
mask   = cv2.dilate(mask, kernel, iterations=1)

# Save mask preview
mask_path = Path(args.output).parent / (Path(args.output).stem + "_mask.png")
cv2.imwrite(str(mask_path), mask * 255)
print(f"      Mask saved → {mask_path}  (coverage: {mask.mean()*100:.1f}%)")

# ── LaMa inpainting ───────────────────────────────────────────────────────────
print("[4/4] Running LaMa inpainting…")

sys.path.insert(0, "/home/digity/lama")
from omegaconf import OmegaConf
from saicinpainting.training.trainers import load_checkpoint

LAMA_CONFIG    = "/home/digity/big-lama/config.yaml"
LAMA_CKPT      = "/home/digity/big-lama/models/best.ckpt"

train_config   = OmegaConf.load(LAMA_CONFIG)
train_config.training_model.predict_only      = True
train_config.visualizer.kind                  = "noop"

model = load_checkpoint(train_config, LAMA_CKPT, strict=False, map_location=DEVICE)
model.freeze()
model.to(DEVICE)

# Prepare tensors (LaMa expects float32 [0,1])
img_t  = torch.from_numpy(image_rgb.astype(np.float32) / 255.0)
img_t  = img_t.permute(2, 0, 1).unsqueeze(0).to(DEVICE)          # 1×3×H×W

msk_t  = torch.from_numpy(mask.astype(np.float32))
msk_t  = msk_t.unsqueeze(0).unsqueeze(0).to(DEVICE)               # 1×1×H×W

batch  = {"image": img_t, "mask": msk_t}

with torch.no_grad():
    result = model(batch)

output = result["inpainted"][0].permute(1, 2, 0).cpu().numpy()
output = np.clip(output * 255, 0, 255).astype(np.uint8)
output_bgr = cv2.cvtColor(output, cv2.COLOR_RGB2BGR)

cv2.imwrite(args.output, output_bgr)
print(f"\n✓ Done — result saved → {args.output}")

# Show comparison
compare = np.hstack([image_bgr, output_bgr])
cv2.imwrite(str(Path(args.output).parent / (Path(args.output).stem + "_compare.png")), compare)
print(f"✓ Comparison (original | result) → {Path(args.output).stem}_compare.png")
