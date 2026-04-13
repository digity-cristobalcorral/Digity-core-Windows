#!/usr/bin/env python3
"""
prepare_genesis_dataset.py

Prepara el dataset de entrenamiento para GENESIS (pix2pix).

Para cada par (gloveN, noN):
  - Extrae los frames RGB de cada sesión
  - Los alinea proporcionalmente (mismo orden temporal)
  - Genera imágenes side-by-side: [glove | clean]
  - Split automático train/val (90/10)

Salida:
  dataset/
    train/   ← imágenes side-by-side para entrenamiento
    val/     ← imágenes side-by-side para validación

Uso:
  python3 tools/prepare_genesis_dataset.py
"""

import os
import sys
import random
from pathlib import Path

import cv2
import numpy as np

# ── Configuración ─────────────────────────────────────────────────────────────

SESSION_ROOT = Path("/mnt/data/session")
OUTPUT_DIR   = Path("/home/digity/genesis_dataset")
IMG_SIZE     = 512          # resolución de cada mitad (512x512 → imagen final 1024x512)
VAL_RATIO    = 0.1          # 10% para validación
RANDOM_SEED  = 42

# Pares: (carpeta_con_guante, carpeta_sin_guante)
# El script los detecta automáticamente buscando prefijos glove/no
# pero también puedes definirlos manualmente aquí:
MANUAL_PAIRS = []   # dejar vacío para detección automática

# ── Detección automática de pares ─────────────────────────────────────────────

def find_pairs(root: Path):
    """
    Busca sesiones que empiecen por 'glove' y 'no' y las empareja por número.
    glove1_... ↔ no1_...
    glove2_... ↔ no2_...
    etc.
    """
    glove_sessions = {}
    clean_sessions = {}

    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if name.startswith("glove"):
            # extraer número: glove1_... → 1
            num = ""
            for ch in name[5:]:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                glove_sessions[int(num)] = d
        elif name.startswith("no"):
            num = ""
            for ch in name[2:]:
                if ch.isdigit():
                    num += ch
                else:
                    break
            if num:
                clean_sessions[int(num)] = d

    pairs = []
    for n in sorted(glove_sessions):
        if n in clean_sessions:
            pairs.append((glove_sessions[n], clean_sessions[n]))
        else:
            print(f"[WARN] glove{n} no tiene pareja no{n} — saltando")
    return pairs

# ── Utilidades ────────────────────────────────────────────────────────────────

def get_rgb_frames(session_dir: Path):
    pov_dir = session_dir / "frames" / "pov"
    if not pov_dir.exists():
        return []
    frames = sorted(f for f in pov_dir.iterdir() if f.name.endswith("_pov_rgb.png"))
    return frames

def align_frames(glove_frames, clean_frames):
    """
    Alineación proporcional: frame i de glove ↔ frame i*(len_clean/len_glove) de clean.
    Para poses estáticas grabadas en el mismo orden esto es suficiente.
    """
    n = min(len(glove_frames), len(clean_frames))
    ratio = len(clean_frames) / max(len(glove_frames), 1)

    pairs = []
    for i in range(n):
        g = glove_frames[i]
        c = clean_frames[int(i * ratio)]
        pairs.append((g, c))
    return pairs

def make_pair_image(glove_path: Path, clean_path: Path, size: int):
    """
    Carga ambos frames, los redimensiona a (size x size) y los concatena horizontalmente.
    Formato pix2pix: [A=glove | B=clean]
    """
    img_g = cv2.imread(str(glove_path))
    img_c = cv2.imread(str(clean_path))

    if img_g is None or img_c is None:
        return None

    img_g = cv2.resize(img_g, (size, size), interpolation=cv2.INTER_AREA)
    img_c = cv2.resize(img_c, (size, size), interpolation=cv2.INTER_AREA)

    return np.concatenate([img_g, img_c], axis=1)   # [glove | clean]

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    random.seed(RANDOM_SEED)

    # Carpetas de salida
    train_dir = OUTPUT_DIR / "train"
    val_dir   = OUTPUT_DIR / "val"
    train_dir.mkdir(parents=True, exist_ok=True)
    val_dir.mkdir(parents=True, exist_ok=True)

    # Encontrar pares
    if MANUAL_PAIRS:
        pairs = [(Path(g), Path(c)) for g, c in MANUAL_PAIRS]
    else:
        pairs = find_pairs(SESSION_ROOT)

    if not pairs:
        print("[ERROR] No se encontraron pares gloveN / noN en", SESSION_ROOT)
        sys.exit(1)

    print(f"Pares encontrados: {len(pairs)}")
    for g, c in pairs:
        print(f"  {g.name}  ↔  {c.name}")
    print()

    # Procesar cada par
    all_samples = []
    for glove_dir, clean_dir in pairs:
        glove_frames = get_rgb_frames(glove_dir)
        clean_frames = get_rgb_frames(clean_dir)

        if not glove_frames:
            print(f"[WARN] {glove_dir.name}: sin frames RGB — saltando")
            continue
        if not clean_frames:
            print(f"[WARN] {clean_dir.name}: sin frames RGB — saltando")
            continue

        aligned = align_frames(glove_frames, clean_frames)
        print(f"{glove_dir.name} ↔ {clean_dir.name}: {len(aligned)} pares")
        all_samples.extend(aligned)

    print(f"\nTotal pares: {len(all_samples)}")

    # Shuffle y split
    random.shuffle(all_samples)
    n_val   = max(1, int(len(all_samples) * VAL_RATIO))
    n_train = len(all_samples) - n_val
    train_samples = all_samples[:n_train]
    val_samples   = all_samples[n_train:]
    print(f"Train: {n_train}   Val: {n_val}")
    print()

    # Generar imágenes
    def save_split(samples, out_dir, split_name):
        ok = 0
        for idx, (g, c) in enumerate(samples):
            img = make_pair_image(g, c, IMG_SIZE)
            if img is None:
                print(f"[WARN] no se pudo leer par {idx}")
                continue
            out_path = out_dir / f"{split_name}_{idx:05d}.jpg"
            cv2.imwrite(str(out_path), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
            ok += 1
            if ok % 100 == 0:
                print(f"  {split_name}: {ok}/{len(samples)}")
        print(f"  {split_name}: {ok} imágenes guardadas → {out_dir}")

    save_split(train_samples, train_dir, "train")
    save_split(val_samples,   val_dir,   "val")

    print()
    print("Dataset listo:")
    print(f"  {OUTPUT_DIR}/train/  ({n_train} imágenes)")
    print(f"  {OUTPUT_DIR}/val/    ({n_val} imágenes)")
    print()
    print("Siguiente paso — clonar pix2pix y entrenar:")
    print("  cd /home/digity")
    print("  git clone https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix")
    print("  cd pytorch-CycleGAN-and-pix2pix")
    print("  pip install -r requirements.txt")
    print(f"  python train.py --dataroot {OUTPUT_DIR} --name genesis --model pix2pix --direction AtoB --input_nc 3 --output_nc 3 --n_epochs 100 --n_epochs_decay 100")

if __name__ == "__main__":
    main()
