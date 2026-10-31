import unittest

from sitef import catalog, command_info, describe_return


class CatalogTests(unittest.TestCase):
    def test_caller_cannot_change_shared_catalog_behavior(self):
        changed = catalog()
        changed["commands"]["23"]["blocking"] = True
        self.assertFalse(command_info(23)["blocking"])
        self.assertEqual(command_info(999999)["kind"], "unknown")

    def test_unknown_codes_are_preserved_and_special_domains_remain_raw(self):
        result = describe_return(-98765)
        self.assertEqual(result["code"], -98765)
        self.assertFalse(result["known"])
        self.assertEqual(result["kind"], "error")
        self.assertEqual(describe_return(10000)["kind"], "continue")
        self.assertEqual(describe_return(1, "pending-count")["kind"], "raw")
        self.assertEqual(describe_return(1, "configuration")["kind"], "error")

    def test_coverage_is_explicitly_incomplete(self):
        coverage = catalog()["coverage"]
        self.assertFalse(coverage["completeFieldSemantics"])
        self.assertFalse(coverage["completePerFunctionWorkflows"])
        self.assertFalse(coverage["financialTransactionsValidatedAgainstVendor"])
