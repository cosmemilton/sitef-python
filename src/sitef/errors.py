"""Errors carry codes, never an automatic retry instruction."""


class SitefError(Exception):
    def __init__(self, code: str, message: str, *, native_status: int | None = None,
                 uncertain: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.native_status = native_status
        self.uncertain = uncertain

    def __repr__(self) -> str:
        return f"SitefError(code={self.code!r}, uncertain={self.uncertain!r})"
