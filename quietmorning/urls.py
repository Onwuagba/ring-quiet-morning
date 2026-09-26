from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from watch import views, webhook

urlpatterns = [
    path("admin/", admin.site.urls),
    path("webhooks/ring/", webhook.ring_webhook),
    path("healthz/", webhook.healthz, name="healthz"),
    path("", views.home, name="home"),
    path("login/", auth_views.LoginView.as_view(template_name="watch/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", views.signup, name="signup"),
    path("invite/<uuid:token>/", views.accept_invite, name="invite"),
    path("onboarding/<int:step>/", views.onboarding, name="onboarding"),
    path("privacy/", views.privacy_page, name="privacy"),
    path("ack/<uuid:token>/", views.ack, name="ack"),
    path("me/large-type/", views.toggle_large_type, name="large_type"),
    path("push/subscribe/", views.push_subscribe, name="push_subscribe"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
    path("sw.js", views.service_worker, name="sw"),
    path("h/<int:hid>/", views.dashboard, name="dashboard"),
    path("h/<int:hid>/status.json", views.status_json, name="status_json"),
    path("h/<int:hid>/history/", views.audit, name="audit"),
    path("h/<int:hid>/summary/", views.summary_preview, name="summary"),
    path("h/<int:hid>/settings/", views.settings_view, name="settings"),
    path("h/<int:hid>/team/", views.team, name="team"),
    path("h/<int:hid>/me/", views.prefs, name="prefs"),
    path("h/<int:hid>/export/", views.export, name="export"),
    path("h/<int:hid>/delete/", views.delete, name="delete"),
]
