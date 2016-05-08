from django.conf.urls import patterns, include, url
from django.views.generic import RedirectView
import socket

from django.contrib import admin

from earkweb import views

from django.conf import settings

urlpatterns = patterns('',
    url(r'^$', RedirectView.as_view(url='home')),
    url(r'^home/$', views.home, name='home'),
    url(r'^earkweb/version/$', views.version, name='version'),

    url(r'^public', views.public_search, name='public_search'),
    url(r'^indexing_status', views.IndexingStatusList.as_view(), name='indexing_status'),
    url(r'^earkcore/', include('earkcore.urls', namespace="earkcore")),
    url(r'^search/', include('search.urls', namespace="search")),
    url(r'^sipcreator/', include('sipcreator.urls', namespace="sipcreator")),
    url(r'^sip2aip/', include('sip2aip.urls', namespace="sip2aip")),
    url(r'^workflow/', include('workflow.urls', namespace="workflow")),
    url(r'^admin/', include(admin.site.urls)),
    url(r'^accounts/login/$', 'django.contrib.auth.views.login',
       {'template_name': 'admin/login.html'}),
    url(r'^accounts/logout/$', 'django.contrib.auth.views.logout'),
#    url(r'^accounts/login/$', 'django_cas.views.login'),
#    url(r'^accounts/logout/$', 'django_cas.views.logout'),
)

# Development server starts at http://127.0.0.1:8888/ so this rule is adds
# the URL prefix path
if socket.gethostname() != "earkdev":
    urlpatterns = patterns('',
        url(r'^$', RedirectView.as_view(url='earkweb/')),
        url(r'^earkweb/', include(urlpatterns)),
    )