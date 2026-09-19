"""Streaming CSV responses shared by the legacy exports and the quality exports.

One home for the download headers and the row-by-row streaming: the response
body is written and sent one row at a time rather than assembled into a
single string first. ``rows`` may be any iterable, including a generator
reading straight off a database cursor, so an export never has to hold the
whole file — nor the whole result set — in memory (review finding C1a).
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
    rows: Iterable[Sequence[Any]],
    *,
    comment_lines: Iterable[str] = (),
) -> StreamingResponse:
    """A CSV download of ``rows``, preceded by ``comment_lines`` verbatim.

    ``rows`` is iterated exactly once, lazily, while the response body is
    being written: a generator (see `quality_db.iter_probe_results`) is the
    intended shape for anything that can be large, and the rows it yields are
    never collected into a list on the way out.

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
