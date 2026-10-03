"""
URL configuration.

Route order matters here: the API, admin and media routes are registered first,
and a catch-all serving the React SPA shell goes *last* so client-side routes
(``/login``, ``/app/...``, ``/console/...``) resolve on a hard refresh without
swallowing anything server-side.
"""
from django.contrib import admin
from django.urls import include, path, re_path

from core.media_views import serve_media, serve_private_media
from core.views_web import spa_index

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/v1/', include('config.api')),
    path('api-auth/', include('rest_framework.urls')),  # browsable API login
]

# Uploaded media (member photos, KYC documents). On shared hosting Django is
# the only listener, so it serves media too.
#
# The same routes are used with DEBUG on and off, deliberately. This used to
# fall back to django.conf.urls.static.static() in development, which serves
# everything openly — so the gate below would have been enforced only in
# production and a developer would never see a private URL fail.
#
# Order matters: the signed-token route must be matched before the catch-all,
# or "private/<token>/<file>" is read as a literal path under MEDIA_ROOT and
# answered with a 404 from the public branch.
urlpatterns += [
    re_path(r'^media/private/(?P<token>[^/]+)/(?P<filename>[^/]*)$',
            serve_private_media, name='private-media'),
    re_path(r'^media/private/(?P<token>[^/]+)/?$', serve_private_media),
    re_path(r'^media/(?P<path>.*)$', serve_media, name='media'),
]

# SPA catch-all — must stay last, and must not shadow anything above.
# The prefixes are matched with "/ or end-of-path" so that a bare "/admin"
# still reaches Django's APPEND_SLASH redirect instead of being answered with
# the SPA shell, while a client route like "/administrators" is unaffected.
urlpatterns += [
    re_path(r'^(?!(?:api|api-auth|admin|media|static)(?:/|$)).*$', spa_index,
            name='spa'),
]
