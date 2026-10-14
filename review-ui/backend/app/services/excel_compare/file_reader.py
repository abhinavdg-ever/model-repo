from pathlib import Path
import pandas as pd


SUPPORTED_EXCEL = [".xlsx", ".xls", ".csv"]


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
        return pd.read_csv(file_path)

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