"""Test suite for AstralNexus."""

import unittest

# Import test modules - only import modules that actually exist
# to avoid pytest collection failures
try:
    from .test_task_graph import TestTaskGraph
    _has_task_graph = True
except ImportError:
    _has_task_graph = False

try:
    from .test_router import TestRouter
    _has_router = True
except ImportError:
    _has_router = False

# Note: Other test modules (test_memory, test_resource_scheduler, etc.)
# are not yet implemented. Add them here when created:
# from .test_memory import TestMemoryFederation
# from .test_resource_scheduler import TestResourceScheduler
# ... etc.


def suite():
    """Create a test suite with all available tests."""
    test_suite = unittest.TestSuite()
    
    # Add tests from each module that exists
    if _has_task_graph:
        test_suite.addTest(unittest.makeSuite(TestTaskGraph))
    
    if _has_router:
        test_suite.addTest(unittest.makeSuite(TestRouter))
    
    # Add more test suites here as modules are implemented
    # if _has_memory:
    #     test_suite.addTest(unittest.makeSuite(TestMemoryFederation))
    # ... etc.
    
    return test_suite


if __name__ == "__main__":
    # Run all tests
    runner = unittest.TextTestRunner(verbosity=2)
    runner.run(suite())
