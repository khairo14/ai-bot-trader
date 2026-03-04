"""
Custom Tools package.

Add your own tools here by subclassing BaseTool from tools/base.py.
Then register them in TOOL_REGISTRY in tools/__init__.py.

Example:
    from tools.base import BaseTool, ToolOutput
    import pandas as pd

    class MyCustomTool(BaseTool):
        name = "MyCustomTool"
        description = "Your description here"
        tier = "custom"

        def calculate(self, data: pd.DataFrame, **params) -> ToolOutput:
            ...

See docs/tools.md for full custom tool documentation.
"""
