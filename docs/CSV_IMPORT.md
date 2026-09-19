# Bring your own sales data

Vyapaar AI accepts a UTF-8 CSV of up to 3 MB / 25,000 rows. Importing replaces the
single demo workspace only; it does not touch other merchants. It tokenizes customer
identifiers before writing the ledger and never imports contact details for messaging.

## Required data

Each row needs:

- a date: `InvoiceDate`, `Order Date`, `Date`, `Event Time`, or `Transaction Date`
- an amount: `Sales`, `Sales Amount`, `Net Sales Amount`, `Amount`, `Total Amount`,
  or `Revenue`; alternatively, `Quantity` / `Qty` plus `UnitPrice` / `Price`

Optional columns improve analysis:

- customer: `CustomerID`, `Cust ID`, `Customer`, or `Customer Email`
- product: `Description`, `Product Name`, `Product`, `Item`, `SKU`, or `StockCode`
- payment: `Payment Mode`, `Payment Method`, or `Channel`

## Public dataset workflow

For the [UCI Online Retail dataset](https://archive.ics.uci.edu/dataset/352/online+retail),
download the workbook, save a smaller 3 MB CSV sample with `InvoiceDate`, `CustomerID`,
`Description`, `Quantity`, and `UnitPrice`, then select **Bring your data** in the app.

The importer understands this schema directly. UCI data is UK-based, so use it to
demonstrate real transaction analytics; label any Indian-context Kaggle data as synthetic.
