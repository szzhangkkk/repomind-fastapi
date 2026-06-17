#!/usr/bin/env python3
"""
benchmark/generate_questions.py — FastAPI 源码自动出题器

使用 tree-sitter 解析 FastAPI 源码 AST + import graph, 自动生成 4 类评测题:
  1. call_chain    — 调用链追踪:核心函数调用路径
  2. cross_file_dep — 跨文件依赖:import 关系与定义位置
  3. function_locate — 函数定义定位:文件+行号+职责
  4. impact_analysis — 修改影响分析:参数/返回值变更的波及范围

输出:benchmark/questions.jsonl (JSONL 格式,每行一题)
依赖:tree-sitter, tree-sitter-languages (repomind conda env)
用法:conda run -n repomind python3 benchmark/generate_questions.py
"""

import json
import os
import re
import textwrap
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import tree_sitter_languages

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
FASTAPI_ROOT = Path(__file__).resolve().parent.parent / "fastapi"
OUTPUT_FILE = Path(__file__).resolve().parent / "questions.jsonl"
TARGET_TOTAL = 50  # 总题数上限
PER_CATEGORY = {"call_chain": 13, "cross_file_dep": 13, "function_locate": 12, "impact_analysis": 12}

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class FuncInfo:
    """一个函数/方法的元信息"""
    name: str
    file: str          # 相对路径,如 routing.py
    line: int          # 1-based
    end_line: int      # 1-based
    parent_class: Optional[str] = None  # 所属类名
    params: List[str] = field(default_factory=list)
    return_annotation: str = ""
    docstring: str = ""
    body_lines: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)  # 体内调用的函数名

@dataclass
class ImportInfo:
    """一条 import 记录"""
    source_file: str   # 被导入的模块(相对路径)
    imported_names: List[str]  # 导入的名称
    importing_file: str  # 发起导入的文件
    line: int
    is_from_import: bool = True  # from X import Y vs import X


# ---------------------------------------------------------------------------
# AST 解析
# ---------------------------------------------------------------------------

def _get_node_text(node) -> str:
    """获取节点文本"""
    if node.text:
        return node.text.decode("utf-8", errors="replace")
    return ""


def _extract_docstring(node) -> str:
    """从函数体提取 docstring"""
    body = node.child_by_field_name("body")
    if body and body.child_count > 0:
        first = body.children[0]
        if first.type == "expression_statement" and first.child_count > 0:
            expr = first.children[0]
            if expr.type == "string":
                text = _get_node_text(expr)
                # 去掉引号,取前 200 字符
                clean = text.strip("\"'").strip()
                if clean.startswith('"""') or clean.startswith("'''"):
                    clean = clean[3:]
                if clean.endswith('"""') or clean.endswith("'''"):
                    clean = clean[:-3]
                return clean.strip()[:200]
    return ""


# 内置/常见非 FastAPI 函数,过滤掉以提高题目质量
_BUILTIN_CALLS = {
    "isinstance", "issubclass", "getattr", "setattr", "hasattr", "type", "id",
    "len", "range", "enumerate", "zip", "map", "filter", "sorted", "reversed",
    "print", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "any", "all", "min", "max", "abs", "sum", "next", "iter", "super",
    "assert", "append", "extend", "insert", "remove", "pop", "clear",
    "copy", "deepcopy", "update", "get", "items", "keys", "values",
    "join", "split", "strip", "replace", "format", "encode", "decode",
    "startswith", "endswith", "find", "index", "count", "sort",
    "upper", "lower", "capitalize",
}

# Bug2 修复: 标准库模块黑名单 — 从调用链中过滤
_STDLIB_MODULES = {
    "typing", "builtins", "collections", "functools", "itertools",
    "os", "sys", "re", "json", "dataclasses", "inspect", "abc", "enum",
    "io", "pathlib", "copy", "warnings", "logging", "contextlib",
    "asyncio", "concurrent", "types", "textwrap", "uuid", "math",
    "random", "hashlib", "time", "datetime",
}

# 常见标准库裸导入函数名(bare import from stdlib), 用于过滤无模块前缀的 stdlib 调用
_COMMON_STDLIB_NAMES = {
    "get_origin", "get_args", "cast", "overload", "TypeVar", "Generic",
    "Union", "Optional", "Annotated", "Type", "ClassVar", "Final",
    "Literal", "Protocol", "runtime_checkable", "dataclass", "field",
    "asdict", "astuple", "replace", "partial", "wraps", "reduce",
    "lru_cache", "cached_property", "chain", "groupby", "islice",
    "deque", "defaultdict", "OrderedDict", "Counter", "namedtuple",
    "abstractmethod", "abstractproperty", "ABC", "ABCMeta",
    "re_compile", "re_match", "re_search", "re_sub", "re_findall",
    "json_dumps", "json_loads",
    "deepcopy", "copy",
    "sleep", "strftime", "strptime",
    "isinstance", "issubclass", "classmethod", "staticmethod", "property",
    "enumerate", "zip", "map", "filter", "sorted", "reversed", "super",
    "print", "len", "range", "type", "id", "getattr", "setattr", "hasattr",
    "IntEnum", "Enum", "unique", "auto",
    "Path", "PurePosixPath", "PureWindowsPath",
    "Formatter", "TextWrapper",
    "getfullargspec", "signature", "Parameter", "Signature",
    "get_type_hints",
    "is_typeddict",
    "_GenericAlias", "_SpecialForm",
    "copyreg",  # not a call but just in case
    "Queue", "Event", "Lock", "Semaphore",
    "asyncio", "ensure_future", "gather", "wait",
    "isawaitable", "iscoroutine", "iscoroutinefunction",
}


def _collect_stdlib_imports_from_source(
    file_sources: Dict[str, str],
) -> Dict[str, Set[str]]:
    """Bug2 修复: 从源码文本直接解析所有 import, 识别 stdlib 导入名"""
    stdlib_imports: Dict[str, Set[str]] = defaultdict(set)
    import_re = re.compile(
        r"^\s*from\s+([\w.]+)\s+import\s+(.+)$", re.MULTILINE
    )
    for file, src in file_sources.items():
        for m in import_re.finditer(src):
            module = m.group(1)
            names_str = m.group(2)
            base_module = module.split(".")[0]
            if base_module in _STDLIB_MODULES:
                for name in names_str.split(","):
                    name = name.strip().split(" as ")[0].strip()
                    if name and name != "*":
                        stdlib_imports[file].add(name)
    return stdlib_imports


def _should_skip_stdlib_call(
    call_name: str, file_stdlib: Set[str], known_defs: Set[str]
) -> bool:
    """Bug2 修复: 判断调用是否来自标准库 (三层检测)"""
    # 1) dotted: typing.get_origin → prefix 在 stdlib 列表
    if "." in call_name:
        prefix = call_name.split(".")[0]
        if prefix in _STDLIB_MODULES:
            return True
    # 2) bare: 通过解析的 import 检测
    bare = call_name.split(".")[-1] if "." in call_name else call_name
    if bare in file_stdlib:
        return True
    # 3) bare: 常见 stdlib 函数名且在 fastapi 中无定义
    if bare in _COMMON_STDLIB_NAMES and bare not in known_defs:
        return True
    return False


def _should_skip_recursive(func: FuncInfo) -> bool:
    """Bug1 修复: 如果函数体内 >50% 的调用是自身递归, 跳过"""
    if not func.calls:
        return False
    self_calls = sum(1 for c in func.calls if c == func.name)
    return self_calls > len(func.calls) * 0.5


def _extract_calls(node, calls: List[str], depth: int = 0):
    """递归提取函数体中的函数调用(过滤内置函数)"""
    if depth > 30:
        return
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        if func_node:
            text = _get_node_text(func_node)
            # 取最右侧的函数名(bare name)
            if "." in text:
                short = text.split(".")[-1]
            else:
                short = text
            if short and short[0].isalpha() and short not in _BUILTIN_CALLS:
                calls.append(text)  # 存完整文本以支持 stdlib 前缀检测
    for child in node.children:
        _extract_calls(child, calls, depth + 1)


def _get_params(node) -> List[str]:
    """提取函数参数名列表"""
    params = []
    parameters = node.child_by_field_name("parameters")
    if parameters:
        for child in parameters.children:
            if child.type == "identifier":
                params.append(_get_node_text(child))
            elif child.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                for sub in child.children:
                    if sub.type == "identifier":
                        params.append(_get_node_text(sub))
                        break
    return params


def parse_file(filepath: Path, rel_path: str, parser) -> Tuple[List[FuncInfo], List[ImportInfo]]:
    """解析单个 Python 文件,返回函数列表和 import 列表"""
    with open(filepath, "rb") as f:
        source = f.read()

    tree = parser.parse(source)
    root = tree.root_node
    lines = source.decode("utf-8", errors="replace").split("\n")

    functions: List[FuncInfo] = []
    imports: List[ImportInfo] = []

    def walk(node, parent_class: Optional[str] = None):
        if node.type == "import_from_statement":
            # tree-sitter Python: module 是 from 之后、import 之前的 dotted_name
            module_name = ""
            import_keyword_seen = False
            names: List[str] = []
            for child in node.children:
                if child.type == "from":
                    continue
                elif child.type == "import":
                    import_keyword_seen = True
                    continue
                elif child.type == "dotted_name":
                    text = _get_node_text(child)
                    if not import_keyword_seen:
                        module_name = text
                    else:
                        names.append(text)
                elif child.type == "aliased_import":
                    for sub in child.children:
                        if sub.type == "dotted_name":
                            names.append(_get_node_text(sub))
                            break
                elif child.type == "import_list":
                    for sub in child.children:
                        if sub.type == "dotted_name":
                            names.append(_get_node_text(sub))
                        elif sub.type == "aliased_import":
                            for s2 in sub.children:
                                if s2.type == "dotted_name":
                                    names.append(_get_node_text(s2))
                                    break
                elif child.type == "wildcard_import":
                    names.append("*")
            if module_name and names:
                # 仅保留 fastapi 内部 import
                if module_name.startswith("fastapi"):
                    module_path = module_name.replace(".", "/") + ".py"
                    imports.append(ImportInfo(
                        source_file=module_path,
                        imported_names=names,
                        importing_file=rel_path,
                        line=node.start_point[0] + 1,
                        is_from_import=True,
                    ))

        elif node.type == "class_definition":
            class_name_node = node.child_by_field_name("name")
            class_name = _get_node_text(class_name_node) if class_name_node else ""
            # 遍历类体
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    walk(child, parent_class=class_name)

        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            name = _get_node_text(name_node) if name_node else ""
            if not name:
                return

            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            docstring = _extract_docstring(node)
            params = _get_params(node)

            return_node = node.child_by_field_name("return_type")
            return_ann = _get_node_text(return_node) if return_node else ""

            # 提取函数体中的调用
            calls: List[str] = []
            body = node.child_by_field_name("body")
            if body:
                _extract_calls(body, calls)

            # 提取代码行(最多 50 行)
            body_start = node.start_point[0]
            body_end = min(node.end_point[0], body_start + 49)
            body_lines = lines[body_start:body_end + 1]

            functions.append(FuncInfo(
                name=name,
                file=rel_path,
                line=start_line,
                end_line=end_line,
                parent_class=parent_class,
                params=params,
                return_annotation=return_ann,
                docstring=docstring,
                body_lines=body_lines,
                calls=calls,
            ))

        # 继续遍历子节点(但不进入已处理的 class/function body)
        if node.type not in ("class_definition", "function_definition"):
            for child in node.children:
                walk(child, parent_class)

    walk(root)
    return functions, imports


def scan_fastapi_source() -> Tuple[List[FuncInfo], List[ImportInfo], Dict[str, str]]:
    """
    扫描 fastapi/ 目录下所有 .py 文件,返回:
      - 所有函数/方法列表
      - 所有 import 记录
      - 文件相对路径到源码行的映射(用于 context_snippet)
    """
    parser = tree_sitter_languages.get_parser("python")
    all_functions: List[FuncInfo] = []
    all_imports: List[ImportInfo] = []
    file_sources: Dict[str, str] = {}

    for py_file in sorted(FASTAPI_ROOT.rglob("*.py")):
        rel_path = str(py_file.relative_to(FASTAPI_ROOT))
        try:
            funcs, imps = parse_file(py_file, rel_path, parser)
            all_functions.extend(funcs)
            all_imports.extend(imps)
            with open(py_file, "r", encoding="utf-8", errors="replace") as f:
                file_sources[rel_path] = f.read()
        except Exception as e:
            print(f"  [WARN] 跳过 {rel_path}: {e}")

    return all_functions, all_imports, file_sources


# ---------------------------------------------------------------------------
# 出题引擎
# ---------------------------------------------------------------------------

def _make_id(counter: List[int]) -> str:
    counter[0] += 1
    return f"q{counter[0]:03d}"


def _get_context_snippet(file_sources: Dict[str, str], file: str, line: int, max_lines: int = 50) -> str:
    """获取源码片段"""
    source = file_sources.get(file, "")
    if not source:
        return ""
    src_lines = source.split("\n")
    start = max(0, line - 1)
    end = min(len(src_lines), start + max_lines)
    snippet = "\n".join(src_lines[start:end])
    return snippet[:2000]  # 限制长度


def _classify_difficulty(func: FuncInfo) -> str:
    """根据函数复杂度判断难度"""
    call_count = len(func.calls)
    line_count = func.end_line - func.line
    if call_count >= 5 or line_count >= 80:
        return "hard"
    elif call_count >= 2 or line_count >= 20:
        return "medium"
    return "easy"


def _find_call_targets(funcs: List[FuncInfo], func_name: str) -> List[FuncInfo]:
    """找到函数体内调用 func_name 的函数(支持 dotted call 名称)"""
    targets = []
    for f in funcs:
        for c in f.calls:
            # calls 存完整文本(如 self.method), 匹配 bare name
            short = c.split(".")[-1] if "." in c else c
            if short == func_name:
                targets.append(f)
                break
    return targets


def _find_importers(imports: List[ImportInfo], name: str) -> List[ImportInfo]:
    """找到导入了某个名称的所有文件"""
    return [imp for imp in imports if name in imp.imported_names]


def _find_definitions(funcs: List[FuncInfo], name: str) -> List[FuncInfo]:
    """找到名为 name 的所有函数定义"""
    return [f for f in funcs if f.name == name]


# ---- call_chain 题 ----

def generate_call_chain_questions(funcs: List[FuncInfo], imports: List[ImportInfo],
                                   file_sources: Dict[str, str], counter: List[int]) -> List[dict]:
    """生成调用链追踪题 (Bug1: 过滤递归伪装; Bug2: 过滤 stdlib 调用; 质量门: ≥3 个不同被调函数)"""
    questions = []

    # Bug2 修复: 从源码解析每文件 stdlib 导入 + 已知 fastapi 定义集合
    stdlib_imports = _collect_stdlib_imports_from_source(file_sources)
    known_defs = {f.name for f in funcs}

    # 选核心函数: 被多处调用、自身调用链丰富的
    core_funcs = [
        f for f in funcs
        if f.parent_class in ("APIRoute", "APIRouter", "FastAPI", None)
        and len(f.calls) >= 2
        and f.name not in ("__init__", "__post_init__", "matches", "decorator")
        and not f.name.startswith("_")
    ]
    # 也加入一些重要的内部函数
    important_internals = [
        f for f in funcs
        if f.name.startswith("_") and len(f.calls) >= 3
        and f.file in ("routing.py", "dependencies/utils.py", "encoders.py")
    ]
    candidates = core_funcs + important_internals
    # 去重
    seen = set()
    unique_candidates = []
    for f in candidates:
        key = (f.file, f.name, f.line)
        if key not in seen:
            seen.add(key)
            unique_candidates.append(f)

    # 优先选路由和依赖相关的
    priority_keywords = ["add_api_route", "get_request_handler", "serialize_response",
                         "solve_dependencies", "get_dependant", "get_body_field",
                         "include_router", "jsonable_encoder", "run_endpoint_function",
                         "analyze_param", "get_flat_dependant"]
    priority_funcs = [f for f in unique_candidates if f.name in priority_keywords]
    other_funcs = [f for f in unique_candidates if f.name not in priority_keywords]
    ordered = priority_funcs + other_funcs

    filtered_stats = {"recursive": 0, "stdlib_only": 0, "too_few_calls": 0, "no_def": 0}

    for func in ordered:
        if len(questions) >= PER_CATEGORY["call_chain"]:
            break

        # Bug1 修复: 跳过递归伪装函数
        if _should_skip_recursive(func):
            filtered_stats["recursive"] += 1
            continue

        # 构建调用链描述 (Bug2: 过滤 stdlib 调用)
        file_stdlib = stdlib_imports.get(func.file, set())
        call_chain_steps = []
        seen_targets = set()
        for called_name in func.calls:
            # Bug2: 跳过 stdlib 调用 (三层检测)
            if _should_skip_stdlib_call(called_name, file_stdlib, known_defs):
                filtered_stats["stdlib_only"] += 1
                continue
            # 取 bare name 用于查找定义
            short = called_name.split(".")[-1] if "." in called_name else called_name
            if short in seen_targets:
                continue
            seen_targets.add(short)
            defs = _find_definitions(funcs, short)
            if defs:
                d = defs[0]
                loc = f"{d.file}:{d.line}"
                step_desc = f"{short}()({loc})"
                if d.docstring:
                    step_desc += f" — {d.docstring[:80]}"
                call_chain_steps.append(step_desc)
            else:
                call_chain_steps.append(f"{short}()")

        # 质量门: GT 必须包含至少 3 个不同的被调函数
        if len(call_chain_steps) < 3:
            filtered_stats["too_few_calls"] += 1
            continue

        class_ctx = f"类 {func.parent_class}." if func.parent_class else ""
        question = (
            f"在 FastAPI 源码中,{class_ctx}{func.name}() ({func.file}:{func.line}) 被调用后,"
            f"会依次执行哪些关键步骤?请列出主要的调用链。"
        )

        ground_truth_points = []
        for i, step in enumerate(call_chain_steps, 1):
            ground_truth_points.append(f"步骤{i}: 调用 {step}")
        if func.docstring:
            ground_truth_points.append(f"功能说明: {func.docstring[:100]}")
        ground_truth = "; ".join(ground_truth_points[:6])

        # 质量门: GT 至少 3 个独立事实点
        if len(ground_truth_points) < 3:
            filtered_stats["too_few_calls"] += 1
            continue

        snippet_lines = func.body_lines[:50]
        context = "\n".join(snippet_lines) if snippet_lines else ""

        questions.append({
            "id": _make_id(counter),
            "category": "call_chain",
            "question": question,
            "ground_truth": ground_truth,
            "difficulty": _classify_difficulty(func),
            "source_files": [f"{func.file}:{func.line}"],
            "context_snippet": context[:2000],
        })

    print(f"  [call_chain] 过滤统计: 递归伪装={filtered_stats['recursive']}, "
          f"stdlib调用={filtered_stats['stdlib_only']}, "
          f"不足3个调用={filtered_stats['too_few_calls']}")
    return questions


# ---- cross_file_dep 题 ----

def generate_cross_file_dep_questions(funcs: List[FuncInfo], imports: List[ImportInfo],
                                       file_sources: Dict[str, str], counter: List[int]) -> List[dict]:
    """生成跨文件依赖题 (Bug3: 过滤外部库定义)"""
    questions = []

    # Bug3 修复: 收集 fastapi/ 下已知有定义的符号(函数或类)
    defined_in_fastapi: Set[str] = set()
    for f in funcs:
        defined_in_fastapi.add(f.name)
    # 需要额外扫描类定义 — funcs 只含函数不含类
    # 简单方案: 从 file_sources 扫描 class 定义
    class_pattern = re.compile(r"^class\s+(\w+)", re.MULTILINE)
    for file, src in file_sources.items():
        for m in class_pattern.finditer(src):
            defined_in_fastapi.add(m.group(1))

    # 收集被多个文件导入的核心名称
    import_count: Dict[str, List[ImportInfo]] = defaultdict(list)
    for imp in imports:
        for name in imp.imported_names:
            import_count[name].append(imp)

    # 选被 >=2 个文件导入的名称, Bug3: 只保留 fastapi 下有定义的
    popular_names = [
        (name, imps) for name, imps in import_count.items()
        if len(imps) >= 2 and not name.startswith("_") and name in defined_in_fastapi
    ]
    # 排序: 导入次数多的优先
    popular_names.sort(key=lambda x: -len(x[1]))

    filtered_external = 0

    # 题型 1: X 函数/类定义在哪个文件? 从哪些文件被 import?
    for name, imps in popular_names[:PER_CATEGORY["cross_file_dep"] // 2 + 4]:
        if len(questions) >= PER_CATEGORY["cross_file_dep"]:
            break

        # Bug3 修复: 确认定义位置在 fastapi/ 下
        defs = _find_definitions(funcs, name)
        definition_loc = ""
        def_desc = ""
        if defs:
            d = defs[0]
            definition_loc = f"{d.file}:{d.line}"
            def_desc = f"定义在 {definition_loc}"
            if d.docstring:
                def_desc += f",功能: {d.docstring[:80]}"
        else:
            # 可能是类定义(非函数), 从 file_sources 查找
            for file, src in file_sources.items():
                m = re.search(rf"^class\s+{re.escape(name)}\b", src, re.MULTILINE)
                if m:
                    lineno = src[:m.start()].count("\n") + 1
                    definition_loc = f"{file}:{lineno}"
                    def_desc = f"定义在 {definition_loc} (class)"
                    break
            if not definition_loc:
                # Bug3: 无定义 → 可能是外部库类型, 跳过
                filtered_external += 1
                continue

        importing_files = sorted(set(imp.importing_file for imp in imps))
        import_details = [f"{f}(行{next(i.line for i in imps if i.importing_file == f)})" for f in importing_files[:5]]

        question = (
            f"在 FastAPI 源码中,{name} 定义在哪个文件的哪一行?它被哪些文件导入使用?"
        )

        # 至少 3 个独立事实点: 定义位置、类型/签名、导入文件
        gt_parts = [
            f"定义位置: {definition_loc}",
        ]
        # 事实2: 类型信息(函数签名或 class)
        if defs and defs[0].docstring:
            gt_parts.append(f"功能说明: {defs[0].docstring[:100]}")
        elif defs:
            sig = ", ".join(defs[0].params[:5]) if defs[0].params else "无参数"
            gt_parts.append(f"函数签名: {name}({sig})")
        else:
            gt_parts.append(f"类型: class {name}")
        # 事实3: 导入文件
        gt_parts.append(f"被导入文件({len(importing_files)}个): {', '.join(import_details)}")

        ground_truth = "; ".join(gt_parts)

        # context: 展示几条 import 语句
        snippet_parts = []
        for imp in imps[:3]:
            snippet_parts.append(f"# {imp.importing_file}:{imp.line}")
            if imp.is_from_import:
                snippet_parts.append(f"from ... import {', '.join(imp.imported_names[:5])}")
        context = "\n".join(snippet_parts)

        questions.append({
            "id": _make_id(counter),
            "category": "cross_file_dep",
            "question": question,
            "ground_truth": ground_truth,
            "difficulty": "medium",
            "source_files": [definition_loc] + [f"{imp.importing_file}:{imp.line}" for imp in imps[:3]],
            "context_snippet": context[:2000],
        })

    # 题型 2: 某文件导入了哪些来自特定模块的符号?
    file_import_count: Dict[str, List[ImportInfo]] = defaultdict(list)
    for imp in imports:
        file_import_count[imp.importing_file].append(imp)

    # 按导入名数量排序,选最丰富的文件
    file_name_counts = [(f, sum(len(i.imported_names) for i in imps)) for f, imps in file_import_count.items()]
    file_name_counts.sort(key=lambda x: -x[1])

    for file, _ in file_name_counts:
        if len(questions) >= PER_CATEGORY["cross_file_dep"]:
            break
        file_imps = file_import_count[file]
        all_names = set()
        for imp in file_imps:
            all_names.update(imp.imported_names)
        if len(all_names) < 5:
            continue

        question = (
            f"文件 {file} 从其他 FastAPI 模块中导入了哪些关键符号?请列出至少 5 个并说明其来源。"
        )

        # 按来源分组
        by_source: Dict[str, List[str]] = defaultdict(list)
        for imp in file_imps:
            for n in imp.imported_names:
                by_source[imp.source_file].append(n)

        gt_parts = []
        for src, names in sorted(by_source.items())[:4]:
            gt_parts.append(f"从 {src} 导入: {', '.join(names[:5])}")
        ground_truth = "; ".join(gt_parts)

        # 取文件头部 import 区域
        file_src = file_sources.get(file, "")
        import_lines = []
        for line in file_src.split("\n")[:30]:
            stripped = line.strip()
            if stripped.startswith("from ") or stripped.startswith("import "):
                import_lines.append(stripped)
            elif import_lines and not stripped.startswith("#"):
                break
        context = "\n".join(import_lines[:15])

        questions.append({
            "id": _make_id(counter),
            "category": "cross_file_dep",
            "question": question,
            "ground_truth": ground_truth,
            "difficulty": "easy",
            "source_files": [file],
            "context_snippet": context[:2000],
        })

    print(f"  [cross_file_dep] 过滤统计: 外部库类型={filtered_external}")
    return questions[:PER_CATEGORY["cross_file_dep"]]


# ---- function_locate 题 ----

def generate_function_locate_questions(funcs: List[FuncInfo], imports: List[ImportInfo],
                                        file_sources: Dict[str, str], counter: List[int]) -> List[dict]:
    """生成函数定义定位题 (Bug4: 跳过无 docstring 函数)"""
    questions = []

    # Bug4 修复: 仅选有 docstring 的函数
    candidates = [
        f for f in funcs
        if not f.name.startswith("_")
        and f.name not in ("decorator",)
        and (f.parent_class or len(f.calls) >= 1)
        and f.docstring  # Bug4: 跳过无 docstring 的函数
    ]
    # 优先选重要函数
    important = [
        "APIRouter", "APIRoute", "FastAPI", "Dependant", "Param", "Body", "Query",
        "Path", "Header", "Cookie", "Depends", "Security", "Form", "File",
        "HTTPException", "RequestValidationError", "ResponseValidationError",
    ]
    priority = [f for f in candidates if f.parent_class in important or f.name in important]
    other = [f for f in candidates if f not in priority]
    ordered = priority + other

    seen = set()
    filtered_no_doc = 0
    for func in ordered:
        if len(questions) >= PER_CATEGORY["function_locate"]:
            break
        key = (func.name, func.parent_class)
        if key in seen:
            continue
        seen.add(key)

        class_ctx = f"类 {func.parent_class} 的方法" if func.parent_class else "函数"
        param_str = ", ".join(func.params[:5]) if func.params else "无参数"

        question = (
            f"FastAPI 中 {func.name} 是什么{class_ctx}?它定义在哪个文件、哪一行?"
            f"参数列表是什么?它的主要职责是什么?"
        )

        doc_desc = func.docstring[:120]
        call_desc = f"内部调用了: {', '.join(func.calls[:4])}" if func.calls else "无明显内部调用"

        ground_truth = (
            f"定义位置: {func.file}:{func.line}; "
            f"类型: {class_ctx}; "
            f"参数: ({param_str}); "
            f"职责: {doc_desc}; "
            f"{call_desc}"
        )

        context = _get_context_snippet(file_sources, func.file, func.line, 50)

        questions.append({
            "id": _make_id(counter),
            "category": "function_locate",
            "question": question,
            "ground_truth": ground_truth,
            "difficulty": _classify_difficulty(func),
            "source_files": [f"{func.file}:{func.line}"],
            "context_snippet": context[:2000],
        })

    print(f"  [function_locate] 候选函数数(有docstring): {len(candidates)}")
    return questions


# ---- impact_analysis 题 ----

def generate_impact_analysis_questions(funcs: List[FuncInfo], imports: List[ImportInfo],
                                        file_sources: Dict[str, str], counter: List[int]) -> List[dict]:
    """生成修改影响分析题 (Bug5: GT 列出具体受影响文件+行号+函数名)"""
    questions = []

    # 定义修改场景
    scenarios = [
        {
            "target_func": "get_dependant",
            "target_file": "dependencies/utils.py",
            "change": "将 get_dependant() 的返回类型从 Dependant 改为一个新的 ResolvedDependency 类",
            "description": "修改 get_dependant 的返回类型",
        },
        {
            "target_func": "Dependant",
            "target_file": "dependencies/models.py",
            "change": "在 Dependant dataclass 中新增一个字段 timeout: float = 30.0",
            "description": "给 Dependant 添加新字段",
        },
        {
            "target_func": "jsonable_encoder",
            "target_file": "encoders.py",
            "change": "将 jsonable_encoder 的返回类型从 Any 改为明确的 dict",
            "description": "修改 jsonable_encoder 返回类型",
        },
        {
            "target_func": "serialize_response",
            "target_file": "routing.py",
            "change": "给 serialize_response 新增一个必需参数 content_type: str",
            "description": "给 serialize_response 添加必需参数",
        },
        {
            "target_func": "solve_dependencies",
            "target_file": "dependencies/utils.py",
            "change": "修改 solve_dependencies 的返回值结构,将 errors 字段重命名为 validation_errors",
            "description": "修改 solve_dependencies 返回值字段名",
        },
        {
            "target_func": "Param",
            "target_file": "params.py",
            "change": "给 Param.__init__ 新增一个必需参数 schema_extra: dict",
            "description": "修改 Param 基类构造函数",
        },
        {
            "target_func": "APIRoute.__init__",
            "target_file": "routing.py",
            "change": "将 APIRoute.__init__ 中 response_model 参数的默认值从 Default(None) 改为 None",
            "description": "修改 APIRoute 构造函数默认值",
        },
        {
            "target_func": "analyze_param",
            "target_file": "dependencies/utils.py",
            "change": "将 analyze_param 的返回类型从 ParamDetails 改为 tuple",
            "description": "修改 analyze_param 返回类型",
        },
        {
            "target_func": "get_request_handler",
            "target_file": "routing.py",
            "change": "给 get_request_handler 新增一个参数 middleware_stack: List[Middleware]",
            "description": "给 get_request_handler 添加参数",
        },
        {
            "target_func": "create_model_field",
            "target_file": "utils.py",
            "change": "将 create_model_field 的 name 参数从 str 改为 Optional[str]",
            "description": "修改 create_model_field 参数类型",
        },
        {
            "target_func": "get_body_field",
            "target_file": "dependencies/utils.py",
            "change": "删除 get_body_field 的 embed_body_fields 参数,改为从 flat_dependant 自动推断",
            "description": "删除 get_body_field 的参数",
        },
        {
            "target_func": "FastAPI.__init__",
            "target_file": "applications.py",
            "change": "给 FastAPI.__init__ 新增一个必需参数 api_version: str",
            "description": "给 FastAPI 构造函数添加必需参数",
        },
    ]

    for scenario in scenarios[:PER_CATEGORY["impact_analysis"]]:
        target_func = scenario["target_func"]
        target_file = scenario["target_file"]
        base_name = target_func.split(".")[0]

        # Bug5 修复: 收集具体影响位置 (文件+行号+函数名)
        impact_details: List[Dict[str, str]] = []  # [{file, line, func, reason}]

        # 方法 1: 找直接调用者 — 谁在代码里调用了 base_name?
        callers = _find_call_targets(funcs, base_name)
        for caller in callers:
            if caller.file != target_file:
                impact_details.append({
                    "file": caller.file,
                    "line": str(caller.line),
                    "func": f"{caller.parent_class + '.' if caller.parent_class else ''}{caller.name}",
                    "reason": "直接调用",
                })

        # 方法 2: 找 import 了该符号的文件(非调用者)
        importers = _find_importers(imports, base_name)
        caller_files = {d["file"] for d in impact_details}
        for imp in importers:
            if imp.importing_file != target_file and imp.importing_file not in caller_files:
                # 在 import 文件中找引用了 base_name 的函数
                for func in funcs:
                    if func.file == imp.importing_file:
                        for line in func.body_lines:
                            if base_name in line:
                                impact_details.append({
                                    "file": func.file,
                                    "line": str(func.line),
                                    "func": f"{func.parent_class + '.' if func.parent_class else ''}{func.name}",
                                    "reason": f"通过 import 使用 {base_name}",
                                })
                                break

        # 去重(同一 file+func 只保留一个)
        seen_keys = set()
        deduped = []
        for d in impact_details:
            k = (d["file"], d["func"])
            if k not in seen_keys:
                seen_keys.add(k)
                deduped.append(d)
        impact_details = deduped[:8]

        question = (
            f"假设要对 FastAPI 源码做如下修改:在 {target_file} 中,"
            f"{scenario['change']}。"
            f"请分析这一改动会影响哪些其他文件?需要同步修改哪些地方?"
        )

        # Bug5 修复: GT 列出具体 文件:行号 函数名 — 原因
        if impact_details:
            detail_strs = []
            for d in impact_details[:6]:
                detail_strs.append(f"{d['file']}:{d['line']} {d['func']}() — {d['reason']}")
            ground_truth = (
                f"受影响位置: {'; '.join(detail_strs)}; "
                f"原因: 这些位置通过 import 或函数调用直接依赖 {target_func}; "
                f"修改要点: 上述函数需确保参数/返回值与修改后的 {target_func} 兼容"
            )
            affected_files = [d["file"] for d in impact_details[:3]]
        else:
            ground_truth = (
                f"直接影响范围主要在 {target_file} 内部; "
                f"需检查 {target_file} 内所有调用 {base_name} 的函数确保兼容; "
                f"同时检查所有 import 了 {base_name} 的文件是否需要适配"
            )
            affected_files = []

        # 取目标函数的代码作为 context
        target_defs = [f for f in funcs if f.name == base_name and f.file == target_file]
        if target_defs:
            context = _get_context_snippet(file_sources, target_defs[0].file, target_defs[0].line, 50)
        else:
            context = _get_context_snippet(file_sources, target_file, 1, 30)

        questions.append({
            "id": _make_id(counter),
            "category": "impact_analysis",
            "question": question,
            "ground_truth": ground_truth,
            "difficulty": "hard",
            "source_files": [f"{target_file}"] + [f"{f}" for f in affected_files],
            "context_snippet": context[:2000],
        })

    return questions


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _validate_question(q: dict) -> bool:
    """最终质量验证: 所有题目必须通过"""
    gt = q.get("ground_truth", "")
    source = q.get("source_files", [])

    # GT 不能含"需从 import 语句推断"或"无 docstring"
    if "需从 import 语句推断" in gt or "无 docstring" in gt:
        return False

    # source_files 不能含"需从"前缀
    for s in source:
        if "需从" in str(s):
            return False

    # GT 不能太短 (< 40 字符)
    if len(gt) < 40:
        return False

    return True


def main():
    print("=" * 60)
    print("FastAPI 源码自动出题器 (tree-sitter)")
    print("=" * 60)

    # 1. 扫描源码
    print(f"\n[1/3] 扫描 {FASTAPI_ROOT} ...")
    funcs, imports, file_sources = scan_fastapi_source()
    print(f"  解析完成: {len(funcs)} 个函数/方法, {len(imports)} 条 import, {len(file_sources)} 个文件")

    # 2. 生成题目
    print(f"\n[2/3] 生成题目 (目标 {TARGET_TOTAL} 题) ...")
    counter = [0]
    all_questions: List[dict] = []
    validation_rejected = 0

    q1 = generate_call_chain_questions(funcs, imports, file_sources, counter)
    all_questions.extend(q1)
    print(f"  call_chain: {len(q1)} 题")

    q2 = generate_cross_file_dep_questions(funcs, imports, file_sources, counter)
    all_questions.extend(q2)
    print(f"  cross_file_dep: {len(q2)} 题")

    q3 = generate_function_locate_questions(funcs, imports, file_sources, counter)
    all_questions.extend(q3)
    print(f"  function_locate: {len(q3)} 题")

    q4 = generate_impact_analysis_questions(funcs, imports, file_sources, counter)
    all_questions.extend(q4)
    print(f"  impact_analysis: {len(q4)} 题")

    # 最终质量验证
    validated = []
    for q in all_questions:
        if _validate_question(q):
            validated.append(q)
        else:
            validation_rejected += 1
    all_questions = validated
    print(f"\n  质量验证: 拒绝 {validation_rejected} 道不合格题")

    # 降级逻辑: 某类不够 12 道,降级到 8 道并标注
    cat_counts: Dict[str, int] = defaultdict(int)
    for q in all_questions:
        cat_counts[q["category"]] += 1
    MIN_PER_CAT = 8
    for cat, count in cat_counts.items():
        if count < MIN_PER_CAT:
            print(f"  ⚠️  {cat}: 只有 {count} 道, 不足 {MIN_PER_CAT} 道")

    # 截断到目标数量
    if len(all_questions) > TARGET_TOTAL:
        all_questions = all_questions[:TARGET_TOTAL]

    # 3. 写出
    print(f"\n[3/3] 写入 {OUTPUT_FILE} ...")
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for q in all_questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    print(f"\n✅ 完成! 共 {len(all_questions)} 题写入 {OUTPUT_FILE}")
    print(f"\n各类分布:")
    final_counts: Dict[str, int] = defaultdict(int)
    for q in all_questions:
        final_counts[q["category"]] += 1
    for cat, count in sorted(final_counts.items()):
        tag = " ⚠️ 降级" if count < 12 else ""
        print(f"  {cat}: {count}{tag}")

    # 展示前 3 题
    print(f"\n{'=' * 60}")
    print("前 3 题预览:")
    print("=" * 60)
    for q in all_questions[:3]:
        print(f"\n  [{q['id']}] ({q['category']}, {q['difficulty']})")
        print(f"  Q: {q['question'][:120]}...")
        print(f"  GT: {q['ground_truth'][:120]}...")
        print(f"  Source: {q['source_files']}")


if __name__ == "__main__":
    main()
