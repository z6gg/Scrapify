class AuthError(Exception):
    """logged out, expired, or a captcha wall"""


class PlaylistNotFoundError(Exception):
    """logged in, but the link doesn't lead to a readable playlist"""
