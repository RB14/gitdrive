"""GitDrive exception hierarchy."""


class GitDriveError(Exception):
    """Base exception for all gitdrive errors."""


class AuthenticationError(GitDriveError):
    """Raised when authentication fails or credentials are missing."""


class DriveApiError(GitDriveError):
    """Raised when a Google Drive API call fails."""


class RateLimitError(DriveApiError):
    """Raised when the Drive API rate limit is exceeded."""


class NotFoundError(DriveApiError):
    """Raised when a requested Drive resource is not found."""


class BundleError(GitDriveError):
    """Raised when a git bundle operation fails."""


class ChecksumMismatchError(BundleError):
    """Raised when a bundle checksum doesn't match."""


class BundleVerifyError(BundleError):
    """Raised when git bundle verify fails."""


class ManifestError(GitDriveError):
    """Raised when manifest parsing or validation fails."""


class ConfigError(GitDriveError):
    """Raised when configuration is invalid or missing."""
