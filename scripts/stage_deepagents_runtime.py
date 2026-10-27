#!/usr/bin/env python3
"""准备新的 DA 运行时变体，不改变任何现有上下文。

源码导出和 SOURCE_MANIFEST 必须已存在。此脚本仅在宿主准备镜像材料，不导入候选、启动 Docker 或执行任务。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.stage_deepagents_langgraph import stage as base_stage

OVERLAYS = {
    'multi-user': {
        'execution/__init__.py': 'execution/__init__.py',
        'execution/worker_control.py': 'execution/worker_control.py',
        'runner/__init__.py': 'runner/__init__.py',
        'runner/shared_backend.py': 'runner/shared_backend.py',
        'runner/shared_supervisor.py': 'runner/shared_supervisor.py',
    },
    'instance-cost': {
        'resident_runtime.py': 'adapters/deepagents_langgraph/resident_runtime.py',
    },
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stage(context, repository, variant):
    context, repository = Path(context).resolve(), Path(repository).resolve()
    allowed = repository / 'external/build-context'
    if (variant not in OVERLAYS or not context.is_relative_to(allowed)
            or context == allowed or 'old' in context.parts):
        raise ValueError('需要新的当前运行时上下文和已知变体')
    protected = ('harness-lab', 'Dockerfile', 'LAB_ADAPTER_MANIFEST.json', 'LAB_RUNTIME_MANIFEST.json')
    if any((context / name).exists() or (context / name).is_symlink() for name in protected):
        raise ValueError('准备运行时不能覆盖现有镜像材料')
    # 调用会修改新上下文的准备脚本前，先读取全部新文件内容。
    payloads = {name: (repository / origin).read_bytes() for name, origin in OVERLAYS[variant].items()}
    base_stage(context, repository)
    manifest_file = context / 'LAB_ADAPTER_MANIFEST.json'
    try:
        manifest = json.loads(manifest_file.read_bytes())
        base_export = manifest['export_sha256']
        entries = {entry['path']: entry for entry in manifest['files']}
        sources = []
        for name, data in payloads.items():
            relative = 'harness-lab/' + name
            destination = context / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            entries[relative] = {'path': relative, 'type': 'file', 'size': len(data), 'sha256': sha(data)}
            sources.append({'repository_path': OVERLAYS[variant][name], 'image_material_path': relative,
                            'size': len(data), 'sha256': sha(data)})
        manifest['files'] = sorted(entries.values(), key=lambda item: item['path'])
        manifest['export_sha256'] = sha(json.dumps(manifest['files'], sort_keys=True).encode())
        manifest['runtime_variant'] = variant
        manifest['base_adapter_export_sha256'] = base_export
        manifest_file.write_text(json.dumps(manifest, indent=2) + '\n')
        runtime = {'schema': 'harness-lab-runtime-manifest/1', 'candidate': 'deepagents_langgraph',
            'variant': variant, 'base_adapter_export_sha256': base_export,
            'adapter_export_sha256': manifest['export_sha256'], 'sources': sources,
            'runtime_verified': False, 'capability_verified': False,
            'python_package_root': '/opt/materials/harness-lab'}
        (context / 'LAB_RUNTIME_MANIFEST.json').write_text(json.dumps(runtime, indent=2) + '\n')
        return {'context': str(context), 'variant': variant, 'adapter_export_sha256': manifest['export_sha256'],
                'status': 'materials_staged_not_runtime_verified'}
    except BaseException:
        (context / 'Dockerfile').unlink(missing_ok=True)
        manifest_file.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--variant', choices=tuple(OVERLAYS), required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.context, ROOT, args.variant)))
