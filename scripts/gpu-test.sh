#!/bin/sh
# Prüft, ob Docker die NVIDIA-GPU an Container durchreicht (vor der ersten Installation sinnvoll).
# Aufruf:  sh scripts/gpu-test.sh
set -e
echo "==> Starte Test-Container mit GPU …"
if docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi; then
    echo "OK: Die GPU ist im Container sichtbar."
else
    echo "FEHLER: Keine GPU im Container."
    echo " - Treiber zu alt? Nötig ist Version 570 oder neuer (CUDA 12.8): nvidia-smi zeigt die Version."
    echo " - Windows: NVIDIA-Treiber aktuell? Docker Desktop -> Settings -> Resources -> WSL integration aktiv?"
    echo " - Linux:   NVIDIA Container Toolkit installiert und 'sudo nvidia-ctk runtime configure --runtime=docker'"
    echo "            gefolgt von 'sudo systemctl restart docker' ausgeführt?"
    exit 1
fi
