"""核验源码导出、实验文件和已安装的源码发行包。"""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
from urllib.parse import unquote, urlparse


SOURCE_PINS = {
    "deepagents": ("archive", "0089e635dce525055272883b53bc222f385beac1", "2b0cb8cd05f7faeb8e8745845958056a9e974658fc85f4682643874c99d8a548"),
    "langgraph": ("git", "98043c37abc60374030f960a03a6e0d4c0112643", None),
}
DISTRIBUTIONS = {
    "deepagents": ("0.7.28", "sources/deepagents/libs/deepagents"),
    "langgraph": ("1.2.16", "sources/langgraph/libs/langgraph"),
    "langgraph-checkpoint": ("4.3.0", "sources/langgraph/libs/checkpoint"),
    "langgraph-checkpoint-sqlite": ("3.1.2", "sources/langgraph/libs/checkpoint-sqlite"),
    "langgraph-prebuilt": ("1.1.1", "sources/langgraph/libs/prebuilt"),
    "langgraph-sdk": ("0.4.8", "sources/langgraph/libs/sdk-py"),
}
REGISTRY_VERSIONS = {"mcp": "1.28.1", "langchain-openai": "1.7.2"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_source(materials: Path | None = None, *, source_only: bool = False) -> dict:
    materials = (materials or Path(__file__).resolve().parents[1]).resolve()
    source_bytes = (materials / "SOURCE_MANIFEST.json").read_bytes()
    source = json.loads(source_bytes)
    if source.get("schema") != "harness-lab-source-manifest/1" or source.get("candidate") != "deepagents_langgraph":
        raise ValueError("DeepAgents/LangGraph 源码清单错误")
    sources = source.get("sources", [])
    if len(sources) != len(SOURCE_PINS) or {record.get("id") for record in sources} != set(SOURCE_PINS):
        raise ValueError("源码清单不符合预期")
    for record in sources:
        pin = SOURCE_PINS.get(record.get("id"))
        if pin is None or record.get("kind") != pin[0] or record.get("commit") != pin[1]:
            raise ValueError("固定的源码版本错误")
        if pin[0] == "archive":
            if record.get("sha256") != pin[2] or record.get("observed_archive_sha256") != pin[2] or record.get("dirty") is not None:
                raise ValueError("源码归档的哈希或来源记录不匹配")
        elif record.get("observed_commit") != pin[1] or record.get("dirty") is not False:
            raise ValueError("Git 源码提交或干净导出的来源记录不匹配")

    def verify_inventory(entries: list) -> None:
        if not isinstance(entries, list) or not entries:
            raise ValueError("来源清单为空")
        seen = set()
        for entry in entries:
            relative = entry.get("path")
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or any(part in ("", ".", "..") or part.lower() == "old" for part in relative.split("/")) or relative in seen:
                raise ValueError("来源记录路径不安全或重复")
            seen.add(relative)
            path = materials / relative
            if not path.resolve().is_relative_to(materials):
                raise ValueError("来源记录链接超出镜像材料目录")
            if entry.get("type") == "symlink":
                if not path.is_symlink() or str(path.readlink()) != entry.get("target"):
                    raise ValueError("源码符号链接不匹配")
                content = str(path.readlink()).encode()
            elif entry.get("type") == "file":
                if not path.is_file() or path.is_symlink():
                    raise ValueError("源码文件类型不匹配")
                content = path.read_bytes()
                if len(content) != entry.get("size"):
                    raise ValueError(f"源码大小不匹配：{relative}")
            else:
                raise ValueError("未知的来源记录文件类型")
            if digest(content) != entry.get("sha256"):
                raise ValueError(f"镜像材料哈希不匹配：{relative}")

    verify_inventory(source.get("files"))
    if digest(json.dumps(source["files"], sort_keys=True).encode()) != source.get("export_sha256"):
        raise ValueError("源码清单摘要不匹配")
    provenance = {"candidate": "deepagents_langgraph", "commits": {name: pin[1] for name, pin in SOURCE_PINS.items()}, "source_export_sha256": source["export_sha256"], "source_file_count": len(source["files"])}
    if source_only:
        return provenance
    adapter = json.loads((materials / "LAB_ADAPTER_MANIFEST.json").read_bytes())
    if adapter.get("schema") != "harness-lab-adapter-manifest/1" or adapter.get("candidate") != "deepagents_langgraph" or adapter.get("source_manifest_sha256") != digest(source_bytes):
        raise ValueError("适配器与源码清单不匹配")
    verify_inventory(adapter.get("files"))
    if digest(json.dumps(adapter["files"], sort_keys=True).encode()) != adapter.get("export_sha256"):
        raise ValueError("适配器清单摘要不匹配")
    installed = {}
    for name, (version, relative) in DISTRIBUTIONS.items():
        distribution = importlib.metadata.distribution(name)
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        parsed = urlparse(direct.get("url", ""))
        if distribution.version != version or parsed.scheme != "file" or parsed.netloc or Path(unquote(parsed.path)).resolve() != (materials / relative).resolve() or direct.get("dir_info", {}).get("editable") is True:
            raise ValueError(f"已安装源码发行包的来源记录不匹配：{name}")
        installed[name] = {"version": distribution.version, "source_path": relative, "editable": False}
    registry = {}
    for name, version in REGISTRY_VERSIONS.items():
        if importlib.metadata.version(name) != version:
            raise ValueError(f"已安装注册表发行包版本不匹配：{name}")
        registry[name] = version
    return {**provenance, "adapter_export_sha256": adapter["export_sha256"], "adapter_file_count": len(adapter["files"]), "installed_source_distributions": installed,
        "installed_registry_distributions": registry}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify_source(source_only=args.source_only), sort_keys=True))
