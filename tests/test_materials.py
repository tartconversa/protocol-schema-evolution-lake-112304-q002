import csv
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
EXPECTED={'protocols.csv': (['protocol_id', 'version', 'lab_id', 'effective_from', 'review_state', 'parent_version'], 36), 'schema_fields.csv': (['protocol_id', 'version', 'field_id', 'name', 'type', 'unit', 'semantic_ref', 'required'], 80), 'run_results.csv': (['run_id', 'protocol_id', 'version', 'field_id', 'value', 'unit', 'recorded_at'], 160), 'mappings.csv': (['mapping_id', 'source_version', 'source_field', 'target_version', 'target_field', 'transform_note', 'approval_state'], 60)}

class MaterialTests(unittest.TestCase):
    def test_csv_contract(self):
        for filename,(columns,count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT/"data"/filename).open(encoding="utf-8-sig",newline="") as stream:
                    reader=csv.DictReader(stream); rows=list(reader)
                self.assertEqual(reader.fieldnames,columns)
                self.assertEqual(len(rows),count)
                self.assertTrue(all(None not in row for row in rows))

if __name__=="__main__": unittest.main()
