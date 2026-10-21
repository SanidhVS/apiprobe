class ApiProbeError(Exception):
    """Every expected failure. `code` is a short stable string (PLAN.md section 9)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}
