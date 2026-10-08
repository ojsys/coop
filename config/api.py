"""API v1 URL routing for CooperativeOS."""
from __future__ import annotations

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounts.member_views import (
    MemberDividendViewSet, MemberDocumentSelfViewSet, MemberLoanProductViewSet,
    MemberLoanViewSet, MemberProfileView, MemberSavingsGoalViewSet,
    MemberSavingsProductViewSet, MemberWithdrawalViewSet,
)
from accounts.auth_views import (
    LoginView, PasswordResetConfirmView, PasswordResetRequestView,
)
from accounts.join_views import (
    CooperativeApplicationView, JoinRequestCreateView, JoinRequestViewSet,
    PublicSocietySearchView,
)
from accounts.views import (
    MemberDocumentViewSet, MembershipViewSet, MeView, RoleViewSet,
)
from approvals.views import ApprovalRequestViewSet
from audit.views import AuditLogViewSet
from communications.views import AnnouncementViewSet, NotificationViewSet
from contributions.views import ContributionTypeViewSet, ContributionViewSet
from dividends.views import DividendViewSet
from governance.views import (
    AttendanceViewSet, MeetingViewSet, ResolutionViewSet,
)
from ledger.views import (AccountViewSet, InternalTransferViewSet,
                         JournalViewSet, LedgerEntryViewSet)
from loans.views import (
    LoanProductViewSet, LoanRepaymentViewSet, LoanViewSet,
)
from payments.views import (
    FlutterwaveWebhookView, PaymentEventViewSet, PaystackWebhookView,
    PayoutViewSet, ProviderAccountViewSet, WalletViewSet,
)
from platform_admin.public_views import InvoicePayView, InvoiceVerifyView
from platform_admin.views import (
    DomainViewSet, IncidentViewSet, InvoiceViewSet, NotificationTemplateViewSet,
    OnboardingItemViewSet, PlanViewSet, PlatformTeamViewSet,
    ProviderStatusViewSet, SubscriptionViewSet, SupportTicketViewSet,
)
from core.public_views import (
    PublicBrandingView,
    PublicLegalDocumentView,
    PublicAppDownloadView,
    PublicLegalIndexView,
    PublicPlanCatalogueView,
    PublicPlansView,
    PublicReferenceView,
    PublicSiteContentView,
    PublicSubdivisionsView,
)
from core.reference_views import ReferenceView
from reports.views import ReportsViewSet
from savings.views import (SavingsGoalViewSet, SavingsProductViewSet,
                          WithdrawalViewSet)
from tenants.views import CooperativeViewSet, PlatformViewSet

router = DefaultRouter()
router.register("cooperatives", CooperativeViewSet, basename="cooperative")
router.register("platform", PlatformViewSet, basename="platform")
router.register("members", MembershipViewSet, basename="member")
router.register("member-documents", MemberDocumentViewSet,
                basename="member-document")
router.register("roles", RoleViewSet, basename="role")
router.register("me", MeView, basename="me")
router.register("contribution-types", ContributionTypeViewSet,
                basename="contribution-type")
router.register("contributions", ContributionViewSet, basename="contribution")
router.register("accounts", AccountViewSet, basename="account")
router.register("journals", JournalViewSet, basename="journal")
router.register("ledger-entries", LedgerEntryViewSet, basename="ledger-entry")
router.register("internal-transfers", InternalTransferViewSet,
                basename="internal-transfer")
router.register("provider-accounts", ProviderAccountViewSet,
                basename="provider-account")
# The disbursement wallet: GET /wallet/ for the balance and recent top-ups,
# POST /wallet/topup/ to fund it, POST /wallet/verify/ to confirm a payment.
router.register("wallet", WalletViewSet, basename="wallet")
# Money sent out. `?unsettled=true` lists what is still in flight, and
# POST /payouts/{id}/recheck/ asks the provider what really happened.
router.register("payouts", PayoutViewSet, basename="payout")
router.register("payment-events", PaymentEventViewSet, basename="payment-event")
router.register("meetings", MeetingViewSet, basename="meeting")
router.register("attendances", AttendanceViewSet, basename="attendance")
router.register("resolutions", ResolutionViewSet, basename="resolution")
router.register("announcements", AnnouncementViewSet, basename="announcement")
router.register("notifications", NotificationViewSet, basename="notification")
router.register("audit-logs", AuditLogViewSet, basename="audit-log")
router.register("reports", ReportsViewSet, basename="report")
router.register("savings-products", SavingsProductViewSet,
                basename="savings-product")
router.register("savings-goals", SavingsGoalViewSet, basename="savings-goal")
router.register("withdrawals", WithdrawalViewSet, basename="withdrawal")
router.register("dividends", DividendViewSet, basename="dividend")
router.register("loan-products", LoanProductViewSet, basename="loan-product")
router.register("loans", LoanViewSet, basename="loan")
router.register("loan-repayments", LoanRepaymentViewSet,
                basename="loan-repayment")
router.register("approvals", ApprovalRequestViewSet, basename="approval")
router.register("join-requests", JoinRequestViewSet, basename="join-request")

# Member self-service (scoped to the requesting member's own records)
router.register("me/documents", MemberDocumentSelfViewSet,
                basename="me-document")
router.register("me/loans", MemberLoanViewSet, basename="me-loan")
router.register("me/loan-products", MemberLoanProductViewSet,
                basename="me-loan-product")
router.register("me/savings-goals", MemberSavingsGoalViewSet,
                basename="me-savings-goal")
router.register("me/savings-products", MemberSavingsProductViewSet,
                basename="me-savings-product")
router.register("me/dividends", MemberDividendViewSet, basename="me-dividend")
router.register("me/withdrawals", MemberWithdrawalViewSet,
                basename="me-withdrawal")

# Platform-admin (Startup Ripple) operational surfaces
router.register("plans", PlanViewSet, basename="plan")
router.register("subscriptions", SubscriptionViewSet, basename="subscription")
router.register("invoices", InvoiceViewSet, basename="invoice")
router.register("onboarding-items", OnboardingItemViewSet,
                basename="onboarding-item")
router.register("domains", DomainViewSet, basename="domain")
router.register("provider-status", ProviderStatusViewSet,
                basename="provider-status")
router.register("platform-team", PlatformTeamViewSet, basename="platform-team")
router.register("support-tickets", SupportTicketViewSet,
                basename="support-ticket")
router.register("incidents", IncidentViewSet, basename="incident")
router.register("notification-templates", NotificationTemplateViewSet,
                basename="notification-template")

urlpatterns = [
    path("auth/token/", LoginView.as_view(), name="api-token"),
    path("auth/password-reset/", PasswordResetRequestView.as_view(),
         name="password-reset"),
    path("auth/password-reset/confirm/", PasswordResetConfirmView.as_view(),
         name="password-reset-confirm"),
    path("reference/", ReferenceView.as_view(), name="reference"),
    # Unauthenticated, for the public marketing site.
    path("public/branding/", PublicBrandingView.as_view(),
         name="public-branding"),
    path("public/plans/", PublicPlansView.as_view(), name="public-plans"),
    path("public/plan-catalogue/", PublicPlanCatalogueView.as_view(),
         name="public-plan-catalogue"),
    # Editable marketing copy and imagery, managed from the Django admin.
    path("public/site-content/", PublicSiteContentView.as_view(),
         name="public-site-content"),
    # Policy pages an admin pastes in (privacy notice, terms, and so on).
    # Counts the download, then redirects to the APK or the store listing.
    path("public/app/<str:platform>/", PublicAppDownloadView.as_view(),
         name="public-app-download"),
    path("public/legal/", PublicLegalIndexView.as_view(), name="public-legal"),
    path("public/legal/<slug:slug>/", PublicLegalDocumentView.as_view(),
         name="public-legal-document"),
    # The signup form's catalogue. Public because signup is.
    path("public/reference/", PublicReferenceView.as_view(),
         name="public-reference"),
    path("public/reference/subdivisions/<str:code>/",
         PublicSubdivisionsView.as_view(), name="public-subdivisions"),
    path("public/societies/", PublicSocietySearchView.as_view(),
         name="public-societies"),
    path("public/apply/", CooperativeApplicationView.as_view(),
         name="public-apply"),
    path("public/join/", JoinRequestCreateView.as_view(),
         name="public-join"),
    # The invoice pay link an officer follows from their inbox. `verify` is
    # declared FIRST: <str:pay_token> would otherwise capture the literal
    # "verify" as a token and the verification endpoint would be unreachable.
    path("public/invoice/verify/", InvoiceVerifyView.as_view(),
         name="public-invoice-verify"),
    path("public/invoice/<str:pay_token>/", InvoicePayView.as_view(),
         name="public-invoice-pay"),
    path("me/profile/", MemberProfileView.as_view(), name="me-profile"),
    path("webhooks/paystack/", PaystackWebhookView.as_view(),
         name="webhook-paystack"),
    path("webhooks/flutterwave/", FlutterwaveWebhookView.as_view(),
         name="webhook-flutterwave"),
    path("", include(router.urls)),
]
