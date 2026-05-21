# ============================================================
# Data Quality Pipeline
# GE + Pydantic + Slack Notification
# ============================================================
import sys
import pandas as pd
import requests
import json
import great_expectations as gx
from pydantic import BaseModel, field_validator
from datetime import datetime

# ============================================================
# STEP 1 — Load Data
# ============================================================
df = pd.read_csv("data/amazon_orders.csv", low_memory=False)
print("✅ Data loaded:", df.shape)

# ============================================================
# STEP 2 — Great Expectations Validation
# ============================================================
context = gx.get_context()

data_source = context.data_sources.add_pandas("pandas_source")
data_asset = data_source.add_dataframe_asset("orders_asset")
batch_definition = data_asset.add_batch_definition_whole_dataframe("orders_batch")

suite = context.suites.add(gx.ExpectationSuite(name="orders_suite"))

suite.add_expectation(
    gx.expectations.ExpectColumnValuesToNotBeNull(column="Order ID")
)
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeUnique(column="Order ID")
)
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeBetween(column="Qty", min_value=0)
)
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeBetween(column="Amount", min_value=0)
)
suite.add_expectation(
    gx.expectations.ExpectColumnValuesToBeInSet(
        column="Status",
        value_set=[
            "Shipped", "Shipped - Delivered to Buyer", "Cancelled",
            "Shipped - Returned to Seller", "Shipped - Picked Up",
            "Pending", "Pending - Waiting for Pick Up",
            "Shipped - Returning to Seller", "Shipped - Out for Delivery",
            "Shipped - Rejected by Buyer", "Shipping",
            "Shipped - Lost in Transit", "Shipped - Damaged"
        ]
    )
)
suite.save()

validation_definition = context.validation_definitions.add(
    gx.ValidationDefinition(
        name="orders_validation",
        data=batch_definition,
        suite=suite
    )
)

ge_result = validation_definition.run(batch_parameters={"dataframe": df})

ge_results_list = ge_result.results
ge_passed = sum(1 for r in ge_results_list if r.success)
ge_failed = sum(1 for r in ge_results_list if not r.success)

print(f"✅ GE Validation complete — Passed: {ge_passed}, Failed: {ge_failed}")

# ============================================================
# STEP 3 — Pydantic Validation
# ============================================================
class OrderModel(BaseModel):
    order_id: str
    qty: int
    amount: float
    currency: str
    ship_country: str
    date: str

    @field_validator("order_id")
    @classmethod
    def order_id_must_not_be_empty(cls, v):
        if not v or str(v).strip() == "":
            raise ValueError("order_id must not be empty")
        return v

    @field_validator("qty")
    @classmethod
    def qty_must_be_non_negative(cls, v):
        if v < 0:
            raise ValueError(f"qty must be >= 0, got {v}")
        return v

    @field_validator("amount")
    @classmethod
    def amount_must_be_non_negative(cls, v):
        if v < 0:
            raise ValueError(f"amount must be >= 0, got {v}")
        return v

    @field_validator("currency")
    @classmethod
    def currency_must_be_inr(cls, v):
        if v != "INR":
            raise ValueError(f"currency must be INR, got {v}")
        return v

    @field_validator("ship_country")
    @classmethod
    def country_must_be_in(cls, v):
        if v != "IN":
            raise ValueError(f"ship_country must be IN, got {v}")
        return v

    @field_validator("date")
    @classmethod
    def date_must_be_valid_format(cls, v):
        try:
            datetime.strptime(str(v), "%m-%d-%y")
        except ValueError:
            raise ValueError(f"date format must be %m-%d-%y, got {v}")
        return v

valid_rows = []
invalid_rows = []
error_messages = []

for idx, row in df.iterrows():
    try:
        OrderModel(
            order_id=str(row.get("Order ID", "")),
            qty=int(row.get("Qty", 0)),
            amount=float(row.get("Amount", 0)),
            currency=str(row.get("currency", "")),
            ship_country=str(row.get("ship-country", "")),
            date=str(row.get("Date", ""))
        )
        valid_rows.append(row)
    except Exception as e:
        invalid_rows.append(row)
        error_messages.append({"row": idx, "error": str(e)})

pd.DataFrame(valid_rows).to_csv("valid_rows.csv", index=False)
pd.DataFrame(invalid_rows).to_csv("invalid_rows.csv", index=False)

print(f"✅ Pydantic Validation complete — Valid: {len(valid_rows):,}, Invalid: {len(invalid_rows):,}")

# ============================================================
# STEP 4 — Slack Notification
# ============================================================
import os
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

overall_success = ge_result.success and len(invalid_rows) == 0
overall_status = "✅ PASSED" if overall_success else "🚨 FAILED"

ge_failed_details = ""
for r in ge_results_list:
    if not r.success:
        column = r.expectation_config.kwargs.get("column", "N/A")
        unexpected_count = r.result.get("unexpected_count", "N/A")
        unexpected_pct = r.result.get("unexpected_percent", 0)
        ge_failed_details += (
            f"\n• `{r.expectation_config.type}` → *{column}*"
            f"\n  Unexpected count: {unexpected_count} ({unexpected_pct:.2f}%)"
        )

pydantic_errors = ""
for e in error_messages[:3]:
    pydantic_errors += f"\n• Row {e['row']}: `{e['error'][:80]}...`"

message = {
    "blocks": [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"CI/CD Data Quality Report — {overall_status}"
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Dataset:*\nAmazon Sale Report"},
                {"type": "mrkdwn", "text": f"*Total Rows:*\n{len(df):,}"},
                {"type": "mrkdwn", "text": f"*GE Passed:*\n{ge_passed} expectations"},
                {"type": "mrkdwn", "text": f"*GE Failed:*\n{ge_failed} expectations"},
                {"type": "mrkdwn", "text": f"*Pydantic Valid:*\n{len(valid_rows):,} rows"},
                {"type": "mrkdwn", "text": f"*Pydantic Invalid:*\n{len(invalid_rows):,} rows"}
            ]
        }
    ]
}

if ge_failed_details:
    message["blocks"].append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*GE Failed Expectations:*{ge_failed_details}"}
    })

if pydantic_errors:
    message["blocks"].append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"*Pydantic Sample Errors:*{pydantic_errors}"}
    })

if SLACK_WEBHOOK_URL:
    response = requests.post(
        SLACK_WEBHOOK_URL,
        data=json.dumps(message),
        headers={"Content-Type": "application/json"}
    )
    if response.status_code == 200:
        print("✅ Slack notification sent!")
    else:
        print(f"❌ Slack error: {response.status_code}")

# ============================================================
# STEP 5 — Exit Code
# ============================================================
print("\n" + "="*50)
if overall_success:
    print("🎉 OVERALL RESULT: PASSED")
    sys.exit(0)
else:
    print("🚨 OVERALL RESULT: FAILED")
    sys.exit(1)
