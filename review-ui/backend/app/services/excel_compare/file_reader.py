import io
from pathlib import Path
import pandas as pd


SUPPORTED_EXCEL = [".xlsx", ".xls", ".csv"]


def decode_csv(data):
    """
    CSV bytes as text: UTF-8 (with or without BOM), else cp1252 (Excel on
    Windows), else latin-1, which decodes any byte.
    """
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def read_csv_any_encoding(source):
    """
    pandas.read_csv for a path, bytes or a binary file object, in whatever
    encoding ``decode_csv`` finds.
    """
    if isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    elif isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        data = source.read()
        if isinstance(data, str):
            return pd.read_csv(io.StringIO(data))
    return pd.read_csv(io.StringIO(decode_csv(data)))


def get_files(folder):
    """
    Return all supported tabular files from a folder.
    """
    folder = Path(folder)

    if not folder.exists():
        raise FileNotFoundError(f"Folder does not exist: {folder}")

    return [
        file
        for file in folder.iterdir()
        if file.is_file() and file.suffix.lower() in SUPPORTED_EXCEL
    ]


def read_tabular_file(file_path):
    """
    Read an Excel or CSV file into a pandas DataFrame.
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File does not exist: {file_path}")

    extension = file_path.suffix.lower()

    if extension == ".csv":
        return read_csv_any_encoding(file_path)

    if extension in [".xlsx", ".xls"]:
        return pd.read_excel(file_path)

    raise ValueError(
        f"Unsupported file type: {extension}. "
        f"Supported types: {SUPPORTED_EXCEL}"
    )


def get_file_info(file_path, dataframe):
    """
    Return basic information about an input file.
    """
    return {
        "file_name": Path(file_path).name,
        "file_type": Path(file_path).suffix.lower(),
        "rows": len(dataframe),
        "columns": len(dataframe.columns),
        "column_names": list(dataframe.columns),
    }