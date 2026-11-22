"""Replace a JSON file only after its new contents have been written successfully."""
import json
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def write_json(path: Path, value, logger: logging.Logger) -> bool:
    temp = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix=".dictation-", delete=False) as f:
            temp = Path(f.name)
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(temp, path)
        return True
    except OSError:
        logger.error("json_write_failed")
        return False
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                logger.error("json_temp_cleanup_failed")
