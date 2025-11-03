# tools.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Dict, Any, List, Tuple
import re

class FakeToolset:
    def __init__(self, tool_config: Dict[str, Any], logger=None):
        self.available: List[str] = tool_config.get("available", [])
        self.vuln_kb: List[Dict[str, Any]] = tool_config.get("vuln_kb", [])
        self.selector_rules: Dict[str, Any] = tool_config.get("selector_rules", {})
        self.calc_max_digits: int = tool_config.get("calculator", {}).get("max_digits", 12)
        self.logger = logger

    def _log(self, name: str, detail: str):
        if self.logger and getattr(self.logger, "show_tool_io", True):
            self.logger.log(f"TOOL-{name}", detail)

    # 模拟私有漏洞知识库
    def retrieve_vuln_kb(self, rule_id_or_query: str) -> Dict[str, Any]:
        q = (rule_id_or_query or "").lower()
        hits = []
        for item in self.vuln_kb:
            if item.get("id","").lower() in q:
                hits.append(item)
                continue
            text = " ".join([
                " ".join(item.get("cwe", [])),
                " ".join(item.get("patterns", [])),
                " ".join(item.get("detection_hints", [])),
                " ".join(item.get("fixes", [])),
                item.get("id", "")
            ]).lower()
            if any(tok in text for tok in re.findall(r"[a-z0-9\-\._/]+", q)):
                hits.append(item)
        out = {"tool":"retrieve_vuln_kb","query":rule_id_or_query,"hits":hits}
        self._log("retrieve_vuln_kb", f"return={out}")
        return out

    # 从整文件中筛选可疑片段（启发式）
    def select_suspicious(self, file_path: str, file_content: str, codeql_query: str,
                          context_window: int = 10, max_snippets: int = 3) -> Dict[str, Any]:
        suspicious_keywords: List[str] = self.selector_rules.get("suspicious_keywords", [])
        if context_window <= 0:
            context_window = 10

        lines = file_content.splitlines()
        n = len(lines)

        hit_lines: List[int] = []
        for idx, line in enumerate(lines):
            low = line.lower()
            if any(kw.lower() in low for kw in suspicious_keywords):
                hit_lines.append(idx)

        # 回退：根据 CodeQL 查询里的关键词粗匹配
        if not hit_lines:
            cq_low = (codeql_query or "").lower()
            fallbacks = ["regex", "pattern", "compile", "file", "path", "input", "sanitize"]
            for idx, line in enumerate(lines):
                if any(k in line.lower() for k in fallbacks if k in cq_low):
                    hit_lines.append(idx)

        # 合并片段
        ranges: List[Tuple[int,int]] = []
        for h in hit_lines:
            s = max(0, h - context_window)
            e = min(n-1, h + context_window)
            if ranges and s <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], e))
            else:
                ranges.append((s, e))

        snippets = []
        for (s, e) in ranges[:max_snippets]:
            snippet = "\n".join(lines[s:e+1])
            snippets.append({"start_line": s+1, "end_line": e+1, "text": snippet})

        out = {"tool": "select_suspicious", "file_path": file_path,
               "snippet_count": len(snippets), "snippets": snippets}
        self._log("select_suspicious", f"return_count={len(snippets)}")
        return out

    # 演示用计算器
    def run_calculator(self, expression: str) -> Dict[str, Any]:
        self._log("run_calculator", f"expression={expression}")
        if not re.fullmatch(r"[0-9\+\-\*\/\.\(\)\s]+", expression):
            out = {"tool": "run_calculator", "expression": expression, "error": "Invalid characters."}
            self._log("run_calculator", f"return={out}")
            return out
        try:
            value = eval(expression, {"__builtins__": {}})
            s = str(value)
            if len(s) > self.calc_max_digits:
                s = s[:self.calc_max_digits]
            out = {"tool": "run_calculator", "expression": expression, "result": s}
            self._log("run_calculator", f"return={out}")
            return out
        except Exception as e:
            out = {"tool": "run_calculator", "expression": expression, "error": str(e)}
            self._log("run_calculator", f"return={out}")
            return out

    # 统一分发
    def call(self, name: str, **kwargs) -> Dict[str, Any]:
        if name not in self.available:
            out = {"error": f"Tool `{name}` is not available."}
            self._log("dispatcher", f"return={out}")
            return out
        if name == "retrieve_vuln_kb":
            return self.retrieve_vuln_kb(kwargs.get("rule_or_query",""))
        if name == "select_suspicious":
            return self.select_suspicious(
                kwargs.get("file_path",""),
                kwargs.get("file_content",""),
                kwargs.get("codeql_query",""),
                context_window=kwargs.get("context_window", 10),
                max_snippets=kwargs.get("max_snippets", 3)
            )
        if name == "run_calculator":
            return self.run_calculator(kwargs.get("expression",""))
        out = {"error": f"Unknown tool `{name}`."}
        self._log("dispatcher", f"return={out}")
        return out
