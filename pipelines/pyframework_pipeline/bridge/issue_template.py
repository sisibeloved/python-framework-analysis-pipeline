"""Issue body generation for ASM diff analysis.

Generates the prompt + source + machine code body for each hotspot function.
Handles chunking for long functions and single-platform degradation.

The template follows the real-world pattern from
https://github.com/sisibeloved/spark/issues/1 -- issue content contains ONLY
source code and machine code (NO timing/perf metrics). The embedded prompt
tells the LLM what to produce in its comment.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMMENT_PREFIX_DUAL = "## 跨平台机器码差异分析："
_COMMENT_PREFIX_ARM = "## Kunpeng 机器码分析："
_COMMENT_PREFIX_X86 = "## Zen4 机器码分析："

_PLATFORM_LABEL_ARM = "Kunpeng"
_PLATFORM_LABEL_X86 = "Zen4"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_lines(text: str) -> int:
    """Count the number of non-empty lines in *text*."""
    return sum(1 for line in text.splitlines() if line.strip())


def _truncate_asm(asm: str, max_lines: int) -> str:
    """Truncate assembly text to *max_lines* non-empty lines.

    Returns the (possibly truncated) text. When truncation occurs a footer
    line is appended indicating the total line count and how many were shown.
    """
    lines = asm.splitlines()
    total = len(lines)
    if total <= max_lines:
        return asm

    shown = lines[:max_lines]
    shown.append(
        f"; [截断: 共{total}行，已展示前{max_lines}行]"
    )
    return "\n".join(shown)


def _resolve_component_display(component: str) -> str:
    """Map internal component id to a human-readable display name."""
    _map: dict[str, str] = {
        "cpython": "CPython",
        "glibc": "glibc",
        "kernel": "Kernel",
        "third_party": "Third Party",
        "bridge_runtime": "Bridge Runtime",
    }
    return _map.get(component, component or "Unknown")


def _resolve_category_display(category_l1: str) -> str:
    """Map internal L1 category id to a human-readable display name."""
    _map: dict[str, str] = {
        "interpreter": "Interpreter",
        "memory": "Memory",
        "gc": "GC",
        "object_model": "Object Model",
        "type_operations": "Type Operations",
        "calls_dispatch": "Calls / Dispatch",
        "native_boundary": "Native Boundary",
        "kernel": "Kernel",
        "unknown": "Unknown",
    }
    return _map.get(category_l1, category_l1 or "Unknown")


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _build_dual_prompt(symbol: str, framework: str) -> str:
    """Build the prompt section for dual-platform (ARM + x86) analysis."""
    return (
        f"你是一个CPU微架构性能优化专家和Python软件专家，现在正在进行"
        f"{framework}在Kunpeng（ARM）和AMD Zen（x86）上的性能差异分析。"
        f"阅读本Issue并在本Issue底下评论。根据两个平台的机器码和对应源码"
        f"分析Arm相对x86性能相对劣势的根因，把整个函数对应源码的机器码差异、"
        f"优化机会和优化策略都列出来。\n"
        f"\n"
        f"差异分析应满足：\n"
        f"1. 完整覆盖整个函数对应源码，保留源码和两个平台的机器码\n"
        f"2. 逐行对照分析，无差异行注明\"无差异\"\n"
        f"3. 优化机会中，如相同数量、相同意义的指令，x86执行更高效，也统计进来\n"
        f"\n"
        f"评论格式要求：\n"
        f"- 以 `{_COMMENT_PREFIX_DUAL}{symbol}` 开头\n"
        f"- `### 总览` 节：表格列出每段源码的ARM/x86指令数和差异概要\n"
        f"- 逐行分析节：每段源码配ARM汇编 + x86汇编 + 比较表 + ARM劣势说明\n"
        f"- `### 根因汇总` 节：表格汇总ARM性能劣势来源"
        f"（编号、劣势来源、出现位置、热路径影响、根因类别）\n"
        f"- `### 优化策略` 节：表格列出优化建议"
        f"（编号、优化点、策略、受益方、ARM收益更高的原因、实施方）\n"
        f"- 优化策略仅保留ARM收益比x86高的，注明受益方"
        f"（仅ARM/ARM收益>x86）和实施方"
        f"（CPython/编译器/硬件/OS/Python库/其它）"
    )


def _build_single_prompt(symbol: str, framework: str, platform: str) -> str:
    """Build the prompt for single-platform analysis."""
    comment_prefix = (
        _COMMENT_PREFIX_ARM if platform == "arm"
        else _COMMENT_PREFIX_X86
    )
    platform_display = (
        _PLATFORM_LABEL_ARM if platform == "arm"
        else _PLATFORM_LABEL_X86
    )
    return (
        f"你是一个CPU微架构性能优化专家和Python软件专家，现在正在进行"
        f"{framework}在{platform_display}上的性能分析。"
        f"阅读本Issue并在本Issue底下评论。"
        f"本函数仅在{platform_display}平台出现，请分析该平台机器码的质量和潜在优化点。\n"
        f"\n"
        f"评论格式要求：\n"
        f"- 以 `{comment_prefix}{symbol}` 开头\n"
        f"- `### 总览` 节：列出函数的指令统计和关键特征\n"
        f"- 逐段分析节：源码配对应汇编 + 优化说明\n"
        f"- `### 优化建议` 节：表格列出优化建议"
        f"（编号、优化点、策略、实施方）\n"
        f"- 注明实施方（CPython/编译器/硬件/OS/Python库/其它）"
    )


# ---------------------------------------------------------------------------
# Body builders
# ---------------------------------------------------------------------------

def _build_source_section(source_code: str | None) -> str:
    """Build the source code markdown section."""
    if not source_code:
        return "## 源码\n\n（无源码）\n"
    return f"## 源码\n\n```c\n{source_code}\n```"


def _build_dual_body(
    symbol: str,
    arm_asm: str,
    x86_asm: str,
    source_code: str | None,
    framework: str,
    binary_path: str,
    component: str,
    category_l1: str,
    max_lines: int,
) -> str:
    """Build the full issue body for a dual-platform function."""
    # Truncate long assembly
    arm_truncated = _truncate_asm(arm_asm, max_lines)
    x86_truncated = _truncate_asm(x86_asm, max_lines)

    prompt = _build_dual_prompt(symbol, framework)
    source_section = _build_source_section(source_code)

    parts = [
        f"## 提示词\n\n{prompt}",
        "",
        f"## 组件\n\n- {component}",
        "",
        f"## 分类\n\n- {category_l1}",
        "",
        source_section,
        "",
        "## 机器码",
        "",
        f"### Kunpeng",
        "",
        "```",
        arm_truncated,
        "```",
        "",
        f"### Zen4",
        "",
        "```",
        x86_truncated,
        "```",
    ]

    return "\n".join(parts)


def _build_single_body(
    symbol: str,
    asm: str,
    platform: str,
    source_code: str | None,
    framework: str,
    binary_path: str,
    component: str,
    category_l1: str,
    max_lines: int,
) -> str:
    """Build the full issue body for a single-platform function."""
    truncated = _truncate_asm(asm, max_lines)

    prompt = _build_single_prompt(symbol, framework, platform)
    source_section = _build_source_section(source_code)

    platform_label = (
        _PLATFORM_LABEL_ARM if platform == "arm"
        else _PLATFORM_LABEL_X86
    )

    parts = [
        f"## 提示词\n\n{prompt}",
        "",
        f"## 组件\n\n- {component}",
        "",
        f"## 分类\n\n- {category_l1}",
        "",
        source_section,
        "",
        "## 机器码",
        "",
        f"### {platform_label}",
        "",
        "```",
        truncated,
        "```",
    ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_asm_diff_issue(
    function: dict[str, Any],
    arm_asm: str | None,
    x86_asm: str | None,
    source_code: str | None = None,
    framework_name: str = "CPython 3.14",
    binary_path: str = "",
    max_lines: int = 2000,
) -> dict[str, str]:
    """Build issue title and body for one hotspot function.

    Parameters
    ----------
    function : dict
        Function descriptor from Dataset.functions[].  Must contain at least
        ``symbol``.  May contain ``component`` and ``categoryL1``.
    arm_asm : str | None
        ARM (Kunpeng) assembly text, or ``None`` if unavailable.
    x86_asm : str | None
        x86 (Zen4) assembly text, or ``None`` if unavailable.
    source_code : str | None
        Optional C/Python source code for the function.
    framework_name : str
        Framework display name used in the prompt.
    binary_path : str
        Path to the binary used for objdump (displayed in the issue body).
    max_lines : int
        Maximum assembly lines to include before truncation.

    Returns
    -------
    dict[str, str]
        ``{'title': str, 'body': str}`` ready for issue creation.

    Raises
    ------
    ValueError
        If both *arm_asm* and *x86_asm* are ``None``.
    """
    if arm_asm is None and x86_asm is None:
        raise ValueError(
            "At least one of arm_asm or x86_asm must be provided"
        )

    symbol = function.get("symbol", "<unknown>")
    component = _resolve_component_display(
        function.get("component", "")
    )
    category_l1 = _resolve_category_display(
        function.get("categoryL1", "")
    )

    # Determine mode: dual or single-platform
    if arm_asm is not None and x86_asm is not None:
        # Dual-platform analysis
        title = f"{symbol}跨平台机器码差异分析"
        body = _build_dual_body(
            symbol=symbol,
            arm_asm=arm_asm,
            x86_asm=x86_asm,
            source_code=source_code,
            framework=framework_name,
            binary_path=binary_path,
            component=component,
            category_l1=category_l1,
            max_lines=max_lines,
        )
    elif arm_asm is not None:
        # ARM-only
        title = f"{symbol} ({_PLATFORM_LABEL_ARM} only) 机器码分析"
        body = _build_single_body(
            symbol=symbol,
            asm=arm_asm,
            platform="arm",
            source_code=source_code,
            framework=framework_name,
            binary_path=binary_path,
            component=component,
            category_l1=category_l1,
            max_lines=max_lines,
        )
    else:
        # x86-only
        title = f"{symbol} ({_PLATFORM_LABEL_X86} only) 机器码分析"
        body = _build_single_body(
            symbol=symbol,
            asm=x86_asm,  # type: ignore[arg-type]
            platform="x86",
            source_code=source_code,
            framework=framework_name,
            binary_path=binary_path,
            component=component,
            category_l1=category_l1,
            max_lines=max_lines,
        )

    return {"title": title, "body": body}


def check_chunking(body: str, max_chars: int = 60000) -> dict[str, Any]:
    """Check if body needs chunking due to excessive length.

    Some issue platforms have character limits on issue bodies. This function
    checks whether the generated body exceeds the threshold and reports the
    line count for downstream splitting logic.

    Parameters
    ----------
    body : str
        The issue body text.
    max_chars : int
        Maximum character count before chunking is needed.

    Returns
    -------
    dict
        ``{'needs_chunking': bool, 'line_count': int}``
    """
    line_count = len(body.splitlines())
    needs_chunking = len(body) > max_chars
    return {
        "needs_chunking": needs_chunking,
        "line_count": line_count,
    }
