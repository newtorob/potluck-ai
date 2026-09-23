"""Conservative first-run recommendations; actual inference is the readiness gate."""
import shutil

from potluck_local.services import model_registry
from potluck_local.services.hardware_detect import detect_hardware
from potluck_local.services.model_downloader import _partial_meta_matches
from potluck_local.services.runtime import get_runtime
from potluck_local.utils.paths import models_dir

GIB = 1024 ** 3


def setup_options() -> dict:
    hardware = detect_hardware()
    base = models_dir()
    base.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(base).free
    runtime = get_runtime()
    current = runtime.current_model_id() if runtime.is_loaded() else None
    entries = model_registry.list_models()
    known = {entry['slug'] for entry in entries}
    # Imported/older weights remain useful; don't force a fresh catalog download.
    for directory in base.iterdir():
        file = directory / 'model.gguf'
        if directory.is_dir() and not directory.is_symlink() and directory.name not in known and file.is_file():
            entries.append({'slug': directory.name, 'name': directory.name, 'installed': True,
                            'size_bytes': file.stat().st_size, 'size_on_disk': file.stat().st_size})
    available = hardware.get('ram_available_gb')
    total = hardware.get('ram_total_gb')
    candidates = []
    for entry in entries:
        size = entry.get('size_on_disk') if entry.get('installed') else entry.get('size_bytes')
        if not isinstance(size, (int, float)) or size <= 0:
            continue
        # Reserve working memory for context/runtime. This is an estimate, not a
        # promise that a model or GPU driver will work. The smoke request proves that.
        working = round(size / GIB * 1.2 + 1.5, 1)
        minimum = max(working, float(entry.get('min_ram_gb') or 0))
        loaded = entry['slug'] == current
        reasons = []
        if not loaded and (not isinstance(available, (int, float)) or not isinstance(total, (int, float))):
            reasons.append('Available memory could not be measured.')
        elif not loaded and (available < working or total < minimum):
            reasons.append(f'Needs about {working:g} GiB available RAM and {minimum:g} GiB total RAM.')
        remaining = int(size)
        partial = base / entry['slug'] / 'model.gguf.partial'
        meta = base / entry['slug'] / 'model.gguf.partial.meta'
        if partial.is_file() and _partial_meta_matches(meta, entry.get('download_url', ''), entry.get('sha256', '')):
            remaining = max(0, remaining - partial.stat().st_size)
        required_disk = 0 if entry.get('installed') else remaining + 256 * 1024 ** 2
        if disk < required_disk:
            reasons.append('Not enough free space in the model directory for this download.')
        candidates.append({'id': entry['slug'], 'name': entry.get('name') or entry['slug'],
                           'sizeBytes': int(size), 'installed': bool(entry.get('installed')),
                           'loaded': loaded, 'fits': not reasons, 'reasons': reasons,
                           'estimatedWorkingGiB': working, 'featured': bool(entry.get('is_featured'))})
    suitable = sorted((c for c in candidates if c['fits']),
                      key=lambda c: (not c['loaded'], not c['installed'], not c['featured'], c['sizeBytes'], c['id']))
    return {'schemaVersion': 1, 'hardware': {k: hardware.get(k) for k in (
        'os', 'arch', 'cpu_name', 'cpu_cores_physical', 'ram_total_gb', 'ram_available_gb', 'gpu_name')},
        'diskFreeBytes': disk, 'currentModel': current,
        'recommendedModel': suitable[0]['id'] if suitable else None, 'models': candidates}
