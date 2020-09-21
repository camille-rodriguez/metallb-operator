"""Init mocking for unit tests."""

import mock
import sys

sys.path.append('src')

oci_image = mock.MagicMock()
sys.modules['oci_image'] = oci_image
