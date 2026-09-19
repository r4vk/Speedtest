"""Streaming CSV responses shared by the legacy exports and the quality exports.

One home for the download headers and the row-by-row streaming, so a large
export never builds the whole file in memory.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Iterable, Sequence

from fastapi.responses import StreamingResponse

#: What `csv.writer` emits between rows; comment lines use it too.
LINE_TERMINATOR = "\r\n"


def csv_response(
    filename: str,
    rows: Sequence[Sequence[Any]],
    *,
    comment_lines: Iterable[str] = (),
) -> StreamingResponse:
    """A CSV download of ``rows``, preceded by ``comment_lines`` verbatim.

    ``comment_lines`` carry their own ``#`` marker and are written before the
    header, which is how an export states that part of the range has no raw
    data left (spec §14). They are never quoted or escaped, so they must not
    be built from user input.
    """
    comments = list(comment_lines)

    def iter_csv():
        for line in comments:
            yield line + LINE_TERMINATOR
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in rows:
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    return StreamingResponse(
        iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
