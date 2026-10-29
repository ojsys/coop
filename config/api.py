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
    EmailVerifyResendView, EmailVerifyView, LoginView, PasswordChangeView,
    PasswordResetConfirmView, PasswordResetRequestView,
)
from accounts.data_rights_views import (
    DeletionRequestViewSet, MyDataExportView, MyDeletionRequestView,
    PublicDeletionConfirmView, PublicDeletionRequestView,
)
from accounts.join_views import (
    CooperativeSignupView, JoinLinkView, JoinRequestCreateView,
    JoinRequestViewSet, MyJoinRequestDetailView, MyJoinRequestsView,
    PublicSocietySearchView,
)
from accounts.views import (
    MemberDocumentViewSet, MembershipViewSet, MeView, RoleViewSet,
)
from approvals.views import ApprovalRequestViewSet
from audit.views import AuditLogViewSet
from communications.member_mail_views import MemberEmailViewSet
from platform_admin.legal_views import PlatformLegalDocumentViewSet
from referrals.views import (MyPayoutRequestView, MyReferrerView,
                             PlatformCommissionViewSet, PlatformPayoutViewSet,
                             PlatformProgramView, PlatformReferrerViewSet,
                             PublicProgramView, ReferrerSignupView)
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
    FeeQuoteView, FlutterwaveWebhookView, PaymentEventViewSet,
    PaystackWebhookView, PayoutViewSet, ProviderAccountViewSet, WalletViewSet,
)
from platform_admin.public_views import InvoicePayView, InvoiceVerifyView
from platform_admin.views import (
    DomainViewSet, IncidentViewSet, InvoiceViewSet, NotificationTemplateViewSet,
    OnboardingItemViewSet, PlanViewSet, PlatformTeamViewSet,
    PlatformUserViewSet, ProviderStatusViewSet, SubscriptionViewSet,
    SupportTicketViewSet,
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
from platform_admin.mailing_views import PlatformEmailViewSet
from tenants.verification_views import (
    CooperativeDocumentViewSet, PlatformVerificationViewSet,
    VerificationViewSet,
)
from tenants.views import CooperativeViewSet, PlatformViewSet

router = DefaultRouter()
router.register("cooperatives", CooperativeViewSet, basename="cooperative")
router.register("platform", PlatformViewSet, basename="platform")
router.register("verification", VerificationViewSet, basename="verification")
router.register("cooperative-documents", CooperativeDocumentViewSet,
                basename="cooperative-document")
router.register("platform-verifications", PlatformVerificationViewSet,
                basename="platform-verification")
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
router.register("member-emails", MemberEmailViewSet, basename="member-email")
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
router.register("deletion-requests", DeletionRequestViewSet,
                basename="deletion-request")

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
router.register("platform-referrers", PlatformReferrerViewSet,
                basename="platform-referrer")
router.register("platform-referral-payouts", PlatformPayoutViewSet,
                basename="platform-referral-payout")
router.register("platform-commissions", PlatformCommissionViewSet,
                basename="platform-commission")
router.register("platform-legal", PlatformLegalDocumentViewSet,
                basename="platform-legal")
router.register("platform-emails", PlatformEmailViewSet,
                basename="platform-email")
router.register("platform-users", PlatformUserViewSet,
                basename="platform-user")
router.register("support-tickets", SupportTicketViewSet,
                basename="support-ticket")
router.register("incidents", IncidentViewSet, basename="incident")
router.register("notification-templates", NotificationTemplateViewSet,
                basename="notification-template")

urlpatterns = [
    path("auth/token/", LoginView.as_view(), name="api-token"),
    path("auth/verify-email/", EmailVerifyView.as_view(),
         name="verify-email"),
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
    # Account deletion without the app — the URL Google Play is given.
    path("public/account-deletion/", PublicDeletionRequestView.as_view(),
         name="public-account-deletion"),
    path("public/account-deletion/confirm/",
         PublicDeletionConfirmView.as_view(),
         name="public-account-deletion-confirm"),
    path("public/referral-program/", PublicProgramView.as_view(),
         name="public-referral-program"),
    path("public/refer/", ReferrerSignupView.as_view(), name="public-refer"),
    path("public/apply/", CooperativeSignupView.as_view(),
         name="public-apply"),
    path("public/join/", JoinRequestCreateView.as_view(),
         name="public-join"),
    # A society's invite link, /join/<code> in the app.
    path("public/join/<str:code>/", JoinLinkView.as_view(),
         name="public-join-link"),
    # The invoice pay link an officer follows from their inbox. `verify` is
    # declared FIRST: <str:pay_token> would otherwise capture the literal
    # "verify" as a token and the verification endpoint would be unreachable.
    path("public/invoice/verify/", InvoiceVerifyView.as_view(),
         name="public-invoice-verify"),
    path("public/invoice/<str:pay_token>/", InvoicePayView.as_view(),
         name="public-invoice-pay"),
    path("me/profile/", MemberProfileView.as_view(), name="me-profile"),
    path("me/password/", PasswordChangeView.as_view(), name="me-password"),
    path("me/referrer/", MyReferrerView.as_view(), name="me-referrer"),
    path("me/referrer/payouts/", MyPayoutRequestView.as_view(),
         name="me-referrer-payouts"),
    path("platform-referral-program/", PlatformProgramView.as_view(),
         name="platform-referral-program"),
    path("me/verify-email/", EmailVerifyResendView.as_view(),
         name="me-verify-email"),
    # A person's own data: a copy of all of it, and asking for deletion.
    path("me/data-export/", MyDataExportView.as_view(), name="me-data-export"),
    path("me/deletion-request/", MyDeletionRequestView.as_view(),
         name="me-deletion-request"),
    # Joining another society with the account you already have.
    path("me/join-requests/", MyJoinRequestsView.as_view(),
         name="me-join-requests"),
    path("me/join-requests/<int:pk>/", MyJoinRequestDetailView.as_view(),
         name="me-join-request"),
    path("fee-quote/", FeeQuoteView.as_view(), name="fee-quote"),
    path("webhooks/paystack/", PaystackWebhookView.as_view(),
         name="webhook-paystack"),
    path("webhooks/flutterwave/", FlutterwaveWebhookView.as_view(),
         name="webhook-flutterwave"),
    path("", include(router.urls)),
]
