"""
URL configuration.

Route order matters here: the API, admin and media routes are registered first,
and a catch-all serving the React SPA shell goes *last* so client-side routes
(``/login``, ``/app/...``, ``/console/...``) resolve on a hard refresh without
swallowing anything server-side.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as serve_file

from core.views_web import spa_index

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('config.api')),
    path('api-auth/', include('rest_framework.urls')),  # browsable API login
]

# Uploaded media (member photos, KYC documents).
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # On shared hosting Django is the only listener, so it serves media too.
    # NOTE: these URLs are currently unauthenticated — anyone with the link can
    # fetch a member photo or KYC document. See DEPLOY_CPANEL.md ("Before real
    # member data") before onboarding a live cooperative.
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', serve_file,
                {'document_root': settings.MEDIA_ROOT}),
    ]

# SPA catch-all — must stay last, and must not shadow anything above.
# The prefixes are matched with "/ or end-of-path" so that a bare "/admin"
# still reaches Django's APPEND_SLASH redirect instead of being answered with
# the SPA shell, while a client route like "/administrators" is unaffected.
urlpatterns += [
    re_path(r'^(?!(?:api|api-auth|admin|media|static)(?:/|$)).*$', spa_index,
            name='spa'),
]
