"""API pagination.

Twenty-five rows a page by default, as before, but a client that needs a whole
list — the console's tables search and count on the client — may ask for up to
``max_page_size`` at a time with ``?page_size=`` and follow ``next``.
"""
from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 500
