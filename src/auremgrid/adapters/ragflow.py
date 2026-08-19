from __future__ import annotations


class RAGFlowAdapter:
    name = "local_ragflow_style_projection"
    role = "messy document extraction"
    license = "Apache-2.0"

    def clean(self, content: str) -> str:
        lines: list[str] = []
        for raw in content.splitlines():
            line = " ".join(raw.strip().split())
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith(("ignore previous", "ignore prior", "reveal every", "reveal all")):
                continue
            lines.append(line)
        return chr(10).join(lines)
