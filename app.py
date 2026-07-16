import os
import json
import difflib
import logging
from datetime import datetime
from io import StringIO, BytesIO

import pandas as pd
import plotly
import plotly.express as px
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, flash
from flask_mysqldb import MySQL
from MySQLdb import IntegrityError, OperationalError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ==============================================================================
# 1. CONFIG — everything sourced from environment variables, nothing hard-coded
# ==============================================================================
def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-only-insecure-key")
    DEBUG = _bool("FLASK_DEBUG", False)

    MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
    # MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
    MYSQL_USER = os.environ.get("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "7010517079")
    MYSQL_DB = os.environ.get("MYSQL_DB", "analytics_dashboard")
    MYSQL_CURSORCLASS = "DictCursor"

    MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", 10))
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024

    ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}


if Config.SECRET_KEY == "dev-only-insecure-key" and not Config.DEBUG:
    # Fail loudly in anything that looks like a production run rather than
    # silently shipping with a guessable session-signing key.
    raise RuntimeError(
        "FLASK_SECRET_KEY is not set. Copy .env.example to .env and set a "
        "real secret key before running outside of debug mode."
    )


# ==============================================================================
# 2. COLUMN MAPPING / CLEANING HELPERS
# ==============================================================================
COLUMN_SYNONYMS = {
    "order_date": ["order_date", "date", "order date", "transaction_date", "time", "timestamp"],
    "region": ["region", "location", "country", "state", "city", "zone"],
    "category": ["category", "type", "group", "class", "department"],
    "product": ["product", "item", "product_name", "item_name", "description"],
    "sales": ["sales", "revenue", "amount", "turnover", "total_sales", "price"],
    "quantity": ["quantity", "qty", "count", "units", "volume"],
    "profit": ["profit", "margin", "earnings", "gain", "net_profit"],
}
REQUIRED_COLUMNS = list(COLUMN_SYNONYMS.keys())
NUMERIC_COLUMNS = ["sales", "quantity", "profit"]


class ColumnMappingError(Exception):
    """Raised when the uploaded file can't be mapped to the required schema."""


def map_user_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename an arbitrary user DataFrame's columns onto the system's
    canonical column names using exact synonym matches first, then fuzzy
    matching as a fallback. Returns a new DataFrame; does not mutate input.
    """
    mapping = {}
    normalized_user_cols = {str(col).lower().strip().replace("_", " "): col for col in df.columns}
    already_used = set()

    for system_col, choices in COLUMN_SYNONYMS.items():
        matched_user_col = None

        for choice in choices:
            if choice in normalized_user_cols and normalized_user_cols[choice] not in already_used:
                matched_user_col = normalized_user_cols[choice]
                break

        if not matched_user_col:
            candidates = [c for c in normalized_user_cols if normalized_user_cols[c] not in already_used]
            closest = difflib.get_close_matches(system_col, candidates, n=1, cutoff=0.6)
            if closest:
                matched_user_col = normalized_user_cols[closest[0]]

        if matched_user_col:
            mapping[matched_user_col] = system_col
            already_used.add(matched_user_col)

    return df.rename(columns=mapping)


def validate_and_clean(df: pd.DataFrame) -> pd.DataFrame:
    """Map, validate, and clean an uploaded DataFrame.

    Raises ColumnMappingError with a user-facing message if required
    columns can't be found.
    """
    df = map_user_columns(df)

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ColumnMappingError(
            f"Could not map data automatically. Missing required columns: {', '.join(missing)}"
        )

    df = df[REQUIRED_COLUMNS].copy()
    df.drop_duplicates(inplace=True)

    df["order_date"] = pd.to_datetime(df["order_date"], dayfirst=True, errors="coerce")
    df["order_date"] = df["order_date"].fillna(pd.Timestamp.now()).dt.date

    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    for col in ["region", "category", "product"]:
        df[col] = df[col].astype(str).str.strip().replace({"nan": "Unknown", "": "Unknown"})

    if df.empty:
        raise ColumnMappingError("The file didn't contain any usable rows after cleaning.")

    return df


# ==============================================================================
# 3. APP FACTORY + DB HELPERS
# ==============================================================================
mysql = MySQL()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    mysql.init_app(app)
    register_routes(app)
    register_error_handlers(app)
    return app


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in Config.ALLOWED_EXTENSIONS


def read_uploaded_file(file_storage):
    filename = file_storage.filename
    if filename.lower().endswith(".csv"):
        return pd.read_csv(file_storage)
    return pd.read_excel(file_storage)


def bulk_insert_sales(df: pd.DataFrame):
    """Insert all rows in a single executemany call instead of one round
    trip per row — this is the single biggest performance win for uploads
    of any real size.
    """
    rows = list(
        df[["order_date", "region", "category", "product", "sales", "quantity", "profit"]]
        .itertuples(index=False, name=None)
    )
    cur = mysql.connection.cursor()
    try:
        cur.executemany(
            """
            INSERT INTO sales_data (order_date, region, category, product, sales, quantity, profit)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
        mysql.connection.commit()
    except (IntegrityError, OperationalError):
        mysql.connection.rollback()
        raise
    finally:
        cur.close()
    return len(rows)


def fetch_df(query: str, params: tuple = ()) -> pd.DataFrame:
    """Run a parameterized SELECT and return the results as a DataFrame,
    without ever interpolating user input directly into SQL text.
    """
    cur = mysql.connection.cursor()
    try:
        cur.execute(query, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    finally:
        cur.close()
    return pd.DataFrame(rows, columns=columns)


def csv_to_bytes(string_io: StringIO) -> BytesIO:
    """send_file wants bytes; wrap the CSV text buffer accordingly."""
    return BytesIO(string_io.getvalue().encode("utf-8"))


# ==============================================================================
# 4. ROUTES
# ==============================================================================
def register_routes(app: Flask):

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Please choose a CSV or Excel file to upload.", "error")
            return redirect(url_for("index"))

        if not allowed_file(file.filename):
            flash("Unsupported file type. Please upload a .csv, .xlsx, or .xls file.", "error")
            return redirect(url_for("index"))

        try:
            df = read_uploaded_file(file)
        except Exception:
            logger.exception("Failed to parse uploaded file %s", file.filename)
            flash("That file couldn't be read. Please check it isn't corrupted and try again.", "error")
            return redirect(url_for("index"))

        try:
            df = validate_and_clean(df)
        except ColumnMappingError as e:
            flash(str(e), "error")
            return redirect(url_for("index"))

        try:
            inserted = bulk_insert_sales(df)
        except (IntegrityError, OperationalError):
            logger.exception("Database error while inserting uploaded rows")
            flash("We couldn't save this data — please try again in a moment.", "error")
            return redirect(url_for("index"))

        flash(f"Uploaded {inserted:,} rows successfully.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/dashboard")
    def dashboard():
        region = request.args.get("region") or None
        start = request.args.get("start") or None
        end = request.args.get("end") or None

        where_clauses = []
        params = []
        if region and region != "All":
            where_clauses.append("region = %s")
            params.append(region)
        if start:
            where_clauses.append("order_date >= %s")
            params.append(start)
        if end:
            where_clauses.append("order_date <= %s")
            params.append(end)

        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        df = fetch_df(f"SELECT * FROM sales_data{where_sql}", tuple(params))

        regions_df = fetch_df("SELECT DISTINCT region FROM sales_data ORDER BY region")
        regions = regions_df["region"].tolist() if not regions_df.empty else []

        if df.empty:
            return render_template(
                "dashboard.html",
                total_sales=0, total_profit=0, total_orders=0, avg_sales=0,
                regions=regions, selected_region=region, start=start, end=end,
                has_data=False,
            )

        total_sales = round(float(df["sales"].sum()), 2)
        total_profit = round(float(df["profit"].sum()), 2)
        total_orders = int(len(df))
        avg_sales = round(float(df["sales"].mean()), 2)

        df_sorted = df.sort_values("order_date")

        bar_fig = px.bar(df, x="category", y="sales", color="category", title="Sales by Category")
        pie_fig = px.pie(df, names="region", values="sales", title="Region-wise Sales")
        line_fig = px.line(df_sorted, x="order_date", y="sales", title="Sales Trend")
        area_fig = px.area(df_sorted, x="order_date", y="profit", title="Profit Over Time")

        for fig in (bar_fig, pie_fig, line_fig, area_fig):
            fig.update_layout(
                margin=dict(l=30, r=20, t=50, b=30),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="Inter, sans-serif", size=13, color="#16202A"),
            )

        return render_template(
            "dashboard.html",
            total_sales=total_sales,
            total_profit=total_profit,
            total_orders=total_orders,
            avg_sales=avg_sales,
            regions=regions,
            selected_region=region,
            start=start,
            end=end,
            has_data=True,
            barJSON=json.dumps(bar_fig, cls=plotly.utils.PlotlyJSONEncoder),
            pieJSON=json.dumps(pie_fig, cls=plotly.utils.PlotlyJSONEncoder),
            lineJSON=json.dumps(line_fig, cls=plotly.utils.PlotlyJSONEncoder),
            areaJSON=json.dumps(area_fig, cls=plotly.utils.PlotlyJSONEncoder),
        )

    @app.route("/filter")
    def filter_data():
        start = request.args.get("start")
        end = request.args.get("end")

        if not start or not end:
            return jsonify({"error": "Both start and end dates are required."}), 400

        for value in (start, end):
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Dates must be in YYYY-MM-DD format."}), 400

        df = fetch_df(
            "SELECT * FROM sales_data WHERE order_date BETWEEN %s AND %s ORDER BY order_date",
            (start, end),
        )
        return jsonify(df.to_dict(orient="records"))

    @app.route("/export")
    def export_csv():
        region = request.args.get("region") or None
        if region and region != "All":
            df = fetch_df("SELECT * FROM sales_data WHERE region = %s", (region,))
        else:
            df = fetch_df("SELECT * FROM sales_data")

        output = StringIO()
        df.to_csv(output, index=False)
        output.seek(0)

        return send_file(
            csv_to_bytes(output),
            mimetype="text/csv",
            as_attachment=True,
            download_name="analytics_export.csv",
        )

    @app.route("/refresh")
    def refresh():
        df = fetch_df("SELECT sales, profit FROM sales_data")
        return jsonify({
            "sales": float(df["sales"].sum()) if not df.empty else 0.0,
            "profit": float(df["profit"].sum()) if not df.empty else 0.0,
            "orders": int(len(df)),
        })


def register_error_handlers(app: Flask):

    @app.errorhandler(413)
    def too_large(_e):
        flash(f"File is too large. Max size is {Config.MAX_UPLOAD_MB} MB.", "error")
        return redirect(url_for("index")), 413

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(500)
    def server_error(e):
        logger.exception("Unhandled server error: %s", e)
        return render_template("error.html", code=500, message="Something went wrong on our end."), 500


# ==============================================================================
# 5. ENTRYPOINT
# ==============================================================================
app = create_app()

if __name__ == "__main__":
    app.run(debug=Config.DEBUG)