import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const spec = JSON.parse(await fs.readFile(process.argv[2], "utf8"));
let workbook;
if (spec.input_path) {
  workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(spec.input_path));
} else {
  workbook = Workbook.create();
  const sheet = workbook.worksheets.add("Pricing");
  sheet.showGridLines = false;
  sheet.mergeCells("A1:D1");
  sheet.mergeCells("B3:D3");
  sheet.mergeCells("B4:D4");
  sheet.mergeCells("B5:D5");
  sheet.mergeCells("B14:D14");
  sheet.getRange("A1").values = [["Pricing Sheet"]];
  sheet.getRange("A3:B6").values = [
    ["Seller", spec.seller],
    ["Buyer", spec.buyer],
    ["Opportunity", spec.motion],
    ["Effective date", new Date(`${spec.effective_date}T00:00:00Z`)],
  ];
  sheet.getRange("A8:D11").values = [
    ["Scope", "Quantity", "Unit price", "Extended"],
    ["Primary scope", 1, spec.primary_minor_units / 100, null],
    ["Delivery and contingency", 1, spec.secondary_minor_units / 100, null],
    ["Total", null, null, null],
  ];
  sheet.getRange("D9").formulas = [["=B9*C9"]];
  sheet.getRange("D9:D10").fillDown();
  sheet.getRange("D11").formulas = [["=SUM(D9:D10)"]];
  sheet.getRange("A13:B15").values = [
    ["Currency", spec.currency],
    ["Normalized source", spec.normalized_source_uri],
    ["Synthetic", true],
  ];
  sheet.getRange("A1:D1").format = {
    fill: "#102A43",
    font: { bold: true, color: "#FFFFFF", size: 18 },
    rowHeight: 34,
    verticalAlignment: "center",
  };
  sheet.getRange("A3:A6").format = { font: { bold: true, color: "#52606D" } };
  sheet.getRange("B3:D5").format = { font: { size: 11 } };
  sheet.getRange("A8:D8").format = {
    fill: "#147D92",
    font: { bold: true, color: "#FFFFFF" },
    borders: { preset: "outside", style: "thin", color: "#0E5A68" },
  };
  sheet.getRange("A9:D10").format.borders = {
    insideHorizontal: { style: "thin", color: "#D9E2EC" },
    bottom: { style: "thin", color: "#D9E2EC" },
  };
  sheet.getRange("A11:D11").format = {
    fill: "#E8F1F5",
    font: { bold: true, color: "#102A43" },
    borders: { preset: "doubleBottom", style: "medium", color: "#102A43" },
  };
  sheet.getRange("C9:D11").format.numberFormat = '"$"#,##0.00';
  sheet.getRange("B6").format.numberFormat = "yyyy-mm-dd";
  sheet.getRange("A13:A15").format.font = { bold: true, color: "#52606D" };
  sheet.getRange("B14:D14").format.font = { size: 9, color: "#52606D" };
  sheet.getRange("A1:D15").format.verticalAlignment = "center";
  sheet.getRange("A1:D15").format.wrapText = true;
  sheet.getRange("A1:A15").format.columnWidthPx = 220;
  sheet.getRange("B1:B15").format.columnWidthPx = 190;
  sheet.getRange("C1:C15").format.columnWidthPx = 120;
  sheet.getRange("D1:D15").format.columnWidthPx = 130;
  sheet.getRange("A3:D15").format.autofitRows();
  sheet.getRange("A14:D14").format.rowHeight = 34;
  const inspection = await workbook.inspect({
    kind: "table",
    range: "Pricing!A1:D16",
    include: "values,formulas",
    tableMaxRows: 16,
    tableMaxCols: 4,
    maxChars: 5000,
  });
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 50 },
    summary: "formula error scan",
    maxChars: 2000,
  });
  if (spec.output_path) {
    await fs.mkdir(path.dirname(spec.output_path), { recursive: true });
    await (await SpreadsheetFile.exportXlsx(workbook)).save(spec.output_path);
  }
  process.stdout.write(JSON.stringify({ inspection: inspection.ndjson, errors: errors.ndjson }));
}
if (spec.preview_path) {
  const preview = await workbook.render({
    sheetName: "Pricing",
    range: "A1:D16",
    scale: 2,
    format: "png",
  });
  await fs.writeFile(spec.preview_path, new Uint8Array(await preview.arrayBuffer()));
}
