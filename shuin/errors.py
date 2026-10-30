class LedgerError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class GhProblem(Exception):
    """GitHub read failed. kind is 'auth' or 'infra'. Never a success."""

    def __init__(self, kind: str):
        if kind not in ("auth", "infra"):
            kind = "infra"
        super().__init__(kind)
        self.kind = kind
