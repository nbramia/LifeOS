"""Free-memory readings and the minimums embedding work requires."""
import os
from pathlib import Path

# Minimum free GPU memory (MB) required to run embeddings alongside the LLM.
# Modern sentence-transformer embedding models can peak at 15-22 GB transient
# allocations during forward pass on long sequences, well beyond the model's
# resident weights (3-4 GB). If less than this is free, embedding work must free
# the LLM or stand aside rather than risk a HIP/CUDA OOM that falls back to CPU
# (~10x slower, OOM-risky).
#
# Override with LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB if your embedding model
# / batch size has a different working-set ceiling.
EMBEDDING_MEMORY_THRESHOLD_MB = int(os.environ.get("LIFEOS_EMBEDDING_MEMORY_THRESHOLD_MB", "28000"))

# Minimum free system RAM (MB) required to run embedding phases.
# Model loading temporarily needs ~4 GB system RAM even when targeting GPU.
# Below this threshold, embedding work is skipped to avoid OOM-killing
# other processes (Chrome, Claude Code, etc.).
EMBEDDING_RAM_THRESHOLD_MB = 4_000


def get_available_gpu_memory_mb() -> int | None:
    """Get available GPU memory in MB from AMDGPU sysfs, or None if unavailable."""
    try:
        # AMDGPU exposes VRAM info via sysfs
        for card_dir in Path("/sys/class/drm").iterdir():
            vram_total = card_dir / "device" / "mem_info_vram_total"
            vram_used = card_dir / "device" / "mem_info_vram_used"
            if vram_total.exists() and vram_used.exists():
                total = int(vram_total.read_text().strip())
                used = int(vram_used.read_text().strip())
                return (total - used) // (1024 * 1024)
    except Exception:
        pass
    return None


def get_available_system_ram_mb() -> int | None:
    """Get available system RAM in MB from /proc/meminfo, or None if unavailable."""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                # MemAvailable is in kB
                kb = int(line.split()[1])
                return kb // 1024
    except Exception:
        pass
    return None
