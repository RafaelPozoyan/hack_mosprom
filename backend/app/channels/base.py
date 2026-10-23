from dataclasses import asdict, dataclass, field


@dataclass
class Button:
    text: str
    payload: str | None = None  # callback-кнопка
    url: str | None = None  # кнопка-ссылка


@dataclass
class OutMessage:
    """Сообщение, не зависящее от канала: MAX и демо-чат рендерят его одинаково."""

    text: str = ""
    buttons: list[list[Button]] = field(default_factory=list)
    image: str | None = None  # имя файла в media/
    file: str | None = None  # имя файла в media/
    sources: list[dict] = field(default_factory=list)  # источники ответа ИИ

    def to_payload(self) -> dict:
        return {
            "buttons": [[asdict(b) for b in row] for row in self.buttons],
            "image": self.image,
            "file": self.file,
            "sources": self.sources,
        }


@dataclass
class InEvent:
    """Входящее событие из любого канала, приведённое к одному виду."""

    kind: str  # start | text | callback
    text: str = ""
    payload: str = ""  # payload кнопки или диплинка
    callback_id: str | None = None
    message_mid: str | None = None
