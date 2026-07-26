"""Общие enum-типы каталога для ORM и публичных API-схем."""

import enum


class Difficulty(enum.IntEnum):
    easy = 1
    intermediate = 2
    hard = 3

    def __str__(self) -> str:
        return self.name
