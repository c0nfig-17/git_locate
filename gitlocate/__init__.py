"""git_locate — GitHub reconnaissance *discovery* tool.

git_locate enumerates the "continent" of a target: it locates GitHub
organizations, repositories and users that are plausibly related to a company.
It does NOT scan code or hunt secrets — that job is delegated to the external
tools you wire into the chaining section of the configuration.

Authorized use only. See ETHICAL_NOTICE.
"""

__version__ = "0.1.0"
__tool_name__ = "git_locate"

ETHICAL_NOTICE = (
    "git_locate is intended EXCLUSIVELY for authorized security testing and "
    "bug-bounty reconnaissance. Only enumerate assets belonging to targets you "
    "have explicit written permission to test. You are solely responsible for "
    "complying with GitHub's Terms of Service, applicable laws and the scope of "
    "your engagement. The authors accept no liability for misuse."
)
