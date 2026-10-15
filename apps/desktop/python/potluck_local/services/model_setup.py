"""First-run recommendations; actual inference is the readiness gate.

Whether a model fits is judged from what was measured on this machine when there
is a measurement (services/memory_probe.py, stored with the answer-check proof),
and from a size-based estimate when there is not. `fitBasis` says which. A measured
need gets a margin: 10% of the measurement, never less than half a GiB. What it is
for: on a Mac in use the same small load read 0.3 to 0.4 GiB apart from run to run,
and raising the context from 4096 to 16384 tokens added 0.4 GiB for a 1.5B model.
The margin covers that spread and a modest change of context. It does not cover a
much larger context on a big model (its cache grows with the context) or a program
that starts after the reading; `contextTokens` says what the figure was taken at.

`recommended` is at most three models, best first, one per model: the ones that
passed the answer check here and ran at MIN_TOKENS_PER_SECOND or more (largest
first), then the ones that passed but are slower or have no speed, then unproven
models that fit by the estimate. Those favor the largest catalog tier within the
GPU memory budget, or small models on CPU, then featured and installed models.
This is a capacity heuristic, not a measured speed or quality claim. A model whose
check failed here, or that does not fit, is never in it.

Optional intended-use guidance ranks catalog matches within each proof class,
and within the hardware tier for untested models. General use keeps the original
ordering. The tags are editorial guidance, never task-quality benchmark scores.
"""
import math
import shutil
from typing import Literal

from potluck_local.services import memory_probe, model_evidence, model_registry
from potluck_local.services.hardware_detect import detect_hardware
from potluck_local.services.model_downloader import _partial_meta_matches
from potluck_local.services.runtime import get_runtime
from potluck_local.utils.paths import models_dir

GIB = 1024 ** 3
MIN_TOKENS_PER_SECOND = 15.0
MARGIN_FLOOR_GIB = 0.5
MARGIN_FRACTION = 0.10
MAX_RECOMMENDED = 3
ModelUse = Literal['general', 'coding', 'writing', 'reasoning']


def _up(gib: float) -> float:
    """A need, rounded up to the tenth it is printed in, so the text never says it fits when it does not."""
    return math.ceil(gib * 10 - 1e-9) / 10


def _down(gib: float) -> float:
    return math.floor(gib * 10 + 1e-9) / 10


def _measured(evidence: "model_evidence.Evidence | None") -> dict | None:
    """The stored measurement as the API shows it, or None."""
    if evidence is None or evidence.measurement is None:
        return None
    host = memory_probe.host_need_bytes(evidence.measurement)
    if host is None:
        return None
    gpu = evidence.measurement.get('gpu_bytes')
    speed = (evidence.benchmark or {}).get('tokens_per_sec')
    return {'hostGiB': round(host / GIB, 2),
            'gpuGiB': round(gpu / GIB, 2) if isinstance(gpu, (int, float)) else None,
            'contextTokens': evidence.measurement.get('n_ctx'),
            'tokensPerSecond': speed if isinstance(speed, (int, float)) else None,
            'checkedAt': evidence.measurement.get('measured_at')}


def _speed(evidence: "model_evidence.Evidence | None") -> float | None:
    speed = ((evidence.benchmark or {}).get('tokens_per_sec')) if evidence else None
    return float(speed) if _positive(speed) else None


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _estimate_preference(entry: dict, hardware: dict) -> tuple[int, str | None]:
    """Rank catalog tiers by usable capacity. Unknown imports keep a neutral rank.

    Unified RAM is one pool, never RAM plus VRAM. Reserve a quarter of total
    unified memory, or 1 GiB of dedicated VRAM, for runtime/context/other use.
    Live available RAM is checked separately. These budgets do not measure free
    VRAM or prove that acceleration will work; local answer checks still lead.
    """
    tier = {'small': 1, 'medium': 2, 'large': 3}.get(entry.get('tier'))
    if tier is None:
        return 0, None
    unified = hardware.get('os') == 'Darwin' and hardware.get('arch') == 'arm64'
    capacity = hardware.get('ram_total_gb') if unified else hardware.get('gpu_vram_gb')
    if hardware.get('inference_gpu_available') is False:
        capacity = None
    elif hardware.get('os') == 'Linux':
        capacity = hardware.get('inference_gpu_vram_gb') if hardware.get('inference_gpu_available') is True else None
    budget = max(0, capacity * 0.75 if unified else capacity - 1) if _positive(capacity) else 0
    need = entry.get('min_vram_gb')
    if _positive(need) and need <= budget:
        return -tier, f'Within the estimated GPU memory budget ({budget:g} GiB). Speed is untested.'
    if tier == 1:
        return 0, 'Smaller model for CPU execution; speed is untested.'
    return tier, 'May need CPU execution or partial GPU offload; speed is untested.'


def setup_options(use_case: ModelUse = 'general') -> dict:
    if use_case not in ('general', 'coding', 'writing', 'reasoning'):
        raise ValueError('Unknown model use case')
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
    evidence = model_evidence.gather({e['slug']: base / e['slug'] / 'model.gguf' for e in entries if e.get('installed')})
    # Loading a different model unloads this one first, so what it holds is available again.
    # Its measured need when there is one; otherwise the estimate's model-sized part (the
    # fixed reserve in the estimate is for the system and is not given back).
    freed, freed_by = 0.0, 'estimated'
    if current:
        loaded_entry = next((e for e in entries if e['slug'] == current), None)
        host = memory_probe.host_need_bytes((evidence.get(current) or model_evidence.Evidence()).measurement)
        if host is not None:
            freed, freed_by = host / GIB, 'measured'
        elif loaded_entry is not None:
            size = loaded_entry.get('size_on_disk') if loaded_entry.get('installed') else loaded_entry.get('size_bytes')
            freed = size / GIB * 1.2 if isinstance(size, (int, float)) and size > 0 else 0.0
    candidates = []
    speeds = {}
    preferences = {}
    for entry in entries:
        size = entry.get('size_on_disk') if entry.get('installed') else entry.get('size_bytes')
        if not _positive(size):
            continue
        # Reserve working memory for context/runtime. This is an estimate, not a
        # promise that a model or GPU driver will work. The smoke request proves that.
        working = round(size / GIB * 1.2 + 1.5, 1)
        minimum = max(working, float(entry.get('min_ram_gb') or 0))
        loaded = entry['slug'] == current
        seen = evidence.get(entry['slug'])
        measured = _measured(seen)
        speeds[entry['slug']] = _speed(seen)
        preference, recommendation_reason = _estimate_preference(entry, hardware)
        preferences[entry['slug']] = preference
        tags = entry.get('use_cases')
        # Ordered editorial guidance, strongest fit first, not evaluation scores.
        use_cases = list(dict.fromkeys(u for u in tags if isinstance(u, str) and u in ('coding', 'writing', 'reasoning'))) if isinstance(tags, list) else []
        reasons = []
        if not loaded and (not _positive(total) or not isinstance(available, (int, float))
                           or isinstance(available, bool) or not math.isfinite(available) or available < 0):
            reasons.append('Available memory could not be measured.')
        elif not loaded:
            free_after = available + freed
            after = (f' About {_down(free_after):g} GiB would be free once {current} is unloaded '
                     f'({freed_by} {_up(freed):g} GiB).') if freed else ''
            if measured is not None:
                measured_gib = _up(measured['hostGiB'])
                margin = _up(max(MARGIN_FLOOR_GIB, MARGIN_FRACTION * measured['hostGiB']))
                if free_after < measured_gib + margin:
                    reasons.append(f'Needs about {measured_gib + margin:.1f} GiB free memory: {measured_gib:g} GiB measured on this '
                                   f'machine plus a margin of {margin:g} GiB. {_down(available):g} GiB is free now.{after}')
            elif available + freed < working or total < minimum:
                reasons.append(f'Needs about {working:g} GiB available RAM and {minimum:g} GiB total RAM.{after}')
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
                           'estimatedWorkingGiB': working, 'featured': bool(entry.get('is_featured')),
                           'fitBasis': 'measured' if measured is not None else 'estimate',
                           'measured': measured, 'checked': seen.checked if seen else None,
                           'recommendationReason': recommendation_reason, 'useCases': use_cases})
    usable = [c for c in candidates if c['fits'] and c['checked'] != 'failed']
    suitable = sorted(usable, key=lambda c: (not c['loaded'], not c['installed'], not c['featured'], c['sizeBytes'], c['id']))
    public_hardware = {k: hardware.get(k) for k in (
        'os', 'arch', 'cpu_name', 'cpu_cores_physical', 'ram_total_gb', 'ram_available_gb', 'gpu_name',
        'gpu_vram_gb', 'inference_gpu_available', 'inference_gpu_vram_gb')}
    for key in ('ram_total_gb', 'ram_available_gb', 'gpu_vram_gb', 'inference_gpu_vram_gb'):
        value = public_hardware[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            public_hardware[key] = None
    return {'schemaVersion': 1, 'hardware': public_hardware, 'useCase': use_case,
        'diskFreeBytes': disk, 'currentModel': current,
        'recommendedModel': suitable[0]['id'] if suitable else None,
        'recommended': _recommended(usable, speeds, preferences, use_case), 'models': candidates}


def _recommended(usable: list[dict], speeds: dict, preferences: dict | None = None, use_case: ModelUse = 'general') -> list[dict]:
    """Up to three models, one each, strongest proof first (see the module notes)."""
    def tier(c: dict) -> int:
        speed = speeds.get(c['id'])
        if c['checked'] == 'passed':
            return 0 if speed is not None and speed >= MIN_TOKENS_PER_SECOND else 1
        return 2

    def order(c: dict) -> tuple:
        speed, t = speeds.get(c['id']), tier(c)
        # Catalog guidance breaks ties within evidence/resource classes. It is
        # not a task benchmark and cannot promote a failed or nonfitting model.
        uses = c.get('useCases', [])
        task_rank = 0 if use_case == 'general' else uses.index(use_case) if use_case in uses else 3
        if t == 0:  # a bigger model that still runs well here is the better pick
            return (0, task_rank, -c['sizeBytes'], -speed, c['id'])
        if t == 1:  # passed but slow: the fastest of them; speed unknown after, smaller first
            return (1, task_rank, speed is None, -(speed or 0.0), c['sizeBytes'], c['id'])
        return (2, (preferences or {}).get(c['id'], 0), task_rank, not c['loaded'], not c['featured'],
                not c['installed'], c['sizeBytes'], c['id'])

    basis = {0: 'proven_fast', 1: 'proven_slow', 2: 'estimate'}
    picks = []
    for c in sorted(usable, key=order)[:MAX_RECOMMENDED]:
        t = tier(c)
        picks.append({'id': c['id'], 'basis': 'proven_speed_unknown' if t == 1 and speeds.get(c['id']) is None else basis[t],
                      'tokensPerSecond': speeds.get(c['id'])})
    return picks
