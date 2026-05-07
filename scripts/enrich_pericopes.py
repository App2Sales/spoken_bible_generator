from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import Any


DEFAULT_SCRAPER_DIR = "/Users/samuelbezerrab/Developer/node/bible-scraper-fork"
DEFAULT_VERSION_ID = 1840

NODE_SCRIPT = r"""
const fs = require('fs');
const BibleScraper = require('./lib');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const scraper = new BibleScraper(input.versionId);
const delayMs = Number(input.delayMs || 0);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

(async () => {
  const results = [];
  for (const book of input.books) {
    for (let chapter = 1; chapter <= book.chapters; chapter += 1) {
      const ref = `${book.short_title}.${chapter}`;
      try {
        const pericopes = await scraper.chapterPericopes(ref);
        results.push({
          book_id: book.book_id,
          short_title: book.short_title,
          chapter,
          pericopes,
        });
      } catch (error) {
        results.push({
          book_id: book.book_id,
          short_title: book.short_title,
          chapter,
          error: error && error.message ? error.message : String(error),
        });
      }
      if (delayMs > 0) {
        await sleep(delayMs);
      }
    }
  }
  process.stdout.write(JSON.stringify({ results }));
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Enriquece o SQLite com pericopes vindas do Bible.com.")
    parser.add_argument("--db-path", default="bibles/naa.db")
    parser.add_argument("--scraper-dir", default=DEFAULT_SCRAPER_DIR)
    parser.add_argument("--version-id", type=int, default=DEFAULT_VERSION_ID)
    parser.add_argument("--book", help="Limita por title ou short_title do banco.")
    parser.add_argument("--delay-ms", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Remove pericopes existentes antes de inserir.")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    scraper_dir = Path(args.scraper_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"DB não encontrado: {db_path}")
    if not (scraper_dir / "lib" / "index.js").exists():
        raise FileNotFoundError(f"Scraper não encontrado em: {scraper_dir}")

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        books = load_books(connection, args.book)
        valid_verses = load_valid_verses(connection)

    payload = {
        "versionId": args.version_id,
        "delayMs": args.delay_ms,
        "books": books,
    }
    scraper_output = run_scraper(scraper_dir, payload)
    rows, errors, skipped = build_rows(scraper_output, valid_verses)

    print(f"books={len(books)} chapters={len(scraper_output)} rows={len(rows)} skipped={skipped} errors={len(errors)}")
    for error in errors[:10]:
        print(f"error {error['short_title']}.{error['chapter']}: {error['error']}")
    if len(errors) > 10:
        print(f"... {len(errors) - 10} more errors")

    if args.dry_run:
        for row in rows[:10]:
            print(f"dry-run {row['book_id']} {row['chapter_num']}:{row['verse']} {row['title']}")
        return 0

    if not args.no_backup:
        backup_path = backup_db(db_path)
        print(f"backup={backup_path}")

    with sqlite3.connect(db_path) as connection:
        create_schema(connection)
        if args.force:
            delete_pericopes_for_books(connection, books)
        connection.executemany(
            """
            insert or replace into pericopes (book_id, chapter_num, verse, title, ntitle)
            values (:book_id, :chapter_num, :verse, :title, :ntitle)
            """,
            rows,
        )
        connection.commit()

    print(f"inserted={len(rows)}")
    return 0


def load_books(connection: sqlite3.Connection, wanted: str | None) -> list[dict[str, Any]]:
    rows = connection.execute(
        "select _id, title, short_title, qtd_chapters from books order by _id asc"
    ).fetchall()
    books = []
    normalized_wanted = normalize_text(wanted) if wanted else None
    for row in rows:
        title = str(row["title"] or "")
        short_title = str(row["short_title"] or "")
        if normalized_wanted and normalized_wanted not in {normalize_text(title), normalize_text(short_title)}:
            continue
        books.append(
            {
                "book_id": int(row["_id"]),
                "title": title,
                "short_title": short_title,
                "chapters": int(row["qtd_chapters"]),
            }
        )
    if not books:
        raise ValueError(f"Livro não encontrado: {wanted}")
    return books


def load_valid_verses(connection: sqlite3.Connection) -> set[tuple[int, int, int]]:
    rows = connection.execute(
        """
        select book_id, chapter_num, verse
        from texts
        where coalesce(head, 0) = 0 and verse > 0
        """
    ).fetchall()
    return {(int(row[0]), int(row[1]), int(row[2])) for row in rows}


def run_scraper(scraper_dir: Path, payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["node", "-e", NODE_SCRIPT],
        cwd=scraper_dir,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout or "{}")
    return list(data.get("results") or [])


def build_rows(
    scraper_output: list[dict[str, Any]],
    valid_verses: set[tuple[int, int, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    rows = []
    errors = []
    skipped = 0
    for chapter_result in scraper_output:
        if chapter_result.get("error"):
            errors.append(chapter_result)
            continue

        book_id = int(chapter_result["book_id"])
        chapter_num = int(chapter_result["chapter"])
        seen_verses: set[int] = set()
        for pericope in chapter_result.get("pericopes") or []:
            verse = int(pericope.get("verse") or 0)
            title = " ".join(str(pericope.get("title") or "").split())
            if not verse or not title or verse in seen_verses:
                skipped += 1
                continue
            if (book_id, chapter_num, verse) not in valid_verses:
                skipped += 1
                continue
            seen_verses.add(verse)
            rows.append(
                {
                    "book_id": book_id,
                    "chapter_num": chapter_num,
                    "verse": verse,
                    "title": title,
                    "ntitle": normalize_text(title),
                }
            )
    return rows, errors, skipped


def backup_db(db_path: Path) -> Path:
    backup_path = db_path.with_suffix(f"{db_path.suffix}.backup-before-pericopes")
    if backup_path.exists():
        backup_path = db_path.with_suffix(f"{db_path.suffix}.backup-before-pericopes-{int(time.time())}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        create table if not exists pericopes (
            _id integer primary key,
            book_id integer not null,
            chapter_num integer not null,
            verse integer not null,
            title text not null,
            ntitle text
        )
        """
    )
    connection.execute(
        """
        create unique index if not exists pericopes_unique_start
        on pericopes (book_id, chapter_num, verse)
        """
    )


def delete_pericopes_for_books(connection: sqlite3.Connection, books: list[dict[str, Any]]) -> None:
    book_ids = [int(book["book_id"]) for book in books]
    if not book_ids:
        return
    placeholders = ",".join("?" for _ in book_ids)
    connection.execute(f"delete from pericopes where book_id in ({placeholders})", book_ids)


def normalize_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return normalized.lower().strip()


if __name__ == "__main__":
    raise SystemExit(main())
