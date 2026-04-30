// s3.go — S3 drift watcher.
//
// VarTrack writes config objects under a prefix in S3 and tags each object
// with the VarTrack label set (URL-encoded):
//
//	app.kubernetes.io%2Fmanaged-by=vartrack
//	vartrack.io%2Ftenant=<tenant>
//	vartrack.io%2Fdatasource=<datasource>
//	...
//
// The watcher lists all objects under the configured prefix, reads the tags
// of each object, and includes in the fingerprint only objects tagged with
// managed-by=vartrack.  The fingerprint is computed over (key, ETag) pairs
// so any content change is detected even if the object metadata is intact.
package watcher

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"

	wcfg "watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

// S3Watcher watches an S3 prefix for drift in VarTrack-managed objects.
type S3Watcher struct {
	name     string
	client   *s3.Client
	bucket   string
	prefix   string
	healer   *healer.Healer
	healOpts healer.HealRequest
}

// NewS3Watcher creates an S3 client and returns a ready S3Watcher.
func NewS3Watcher(
	ctx context.Context,
	cfg *dsv1.S3Config,
	rule *models.Rule,
	h *healer.Healer,
) (*S3Watcher, error) {
	if cfg.GetBucket() == "" {
		return nil, fmt.Errorf("s3 watcher %s: bucket must not be empty", wcfg.RuleName(rule))
	}

	awsCfg, err := config.LoadDefaultConfig(ctx,
		config.WithRegion(cfg.GetRegion()),
	)
	if err != nil {
		return nil, fmt.Errorf("s3 watcher %s: load AWS config: %w", wcfg.RuleName(rule), err)
	}

	client := s3.NewFromConfig(awsCfg)

	slog.Info("s3 watcher: configured",
		"watcher", wcfg.RuleName(rule), "bucket", cfg.GetBucket(), "prefix", cfg.GetPrefix())

	return &S3Watcher{
		name:   wcfg.RuleName(rule),
		client: client,
		bucket: cfg.GetBucket(),
		prefix: cfg.GetPrefix(),
		healer: h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
	}, nil
}

// Name implements Watcher.
func (w *S3Watcher) Name() string { return "s3/" + w.name }

// Snapshot lists all objects under the prefix, filters to VarTrack-managed
// ones, and returns a fingerprint of (key, ETag) pairs.
//
// Using the ETag means any content change — even with identical object names
// — is detected.  The ETag is an MD5 of the object body (for simple puts)
// or a composite hash for multipart uploads; either way it changes when
// content changes.
func (w *S3Watcher) Snapshot(ctx context.Context) (string, error) {
	records := make(map[string]string)

	var contToken *string
	for {
		out, err := w.client.ListObjectsV2(ctx, &s3.ListObjectsV2Input{
			Bucket:            aws.String(w.bucket),
			Prefix:            aws.String(w.prefix),
			ContinuationToken: contToken,
		})
		if err != nil {
			return "", fmt.Errorf("s3 snapshot %s: list objects: %w", w.name, err)
		}

		for _, obj := range out.Contents {
			key := aws.ToString(obj.Key)
			etag := aws.ToString(obj.ETag)

			// Skip the watcher state object itself.
			if key == w.prefix+S3StateObject {
				continue
			}

			// Check tags to confirm VarTrack manages this object.
			managed, err := w.isManaged(ctx, key)
			if err != nil {
				slog.Warn("s3 watcher: tag fetch failed",
					"watcher", w.name, "key", key, "error", err)
				continue
			}
			if !managed {
				continue
			}

			records[key] = etag
		}

		if !aws.ToBool(out.IsTruncated) {
			break
		}
		contToken = out.NextContinuationToken
	}

	return FingerprintRecords(records), nil
}

// Restore implements Watcher.
func (w *S3Watcher) Restore(ctx context.Context) error {
	slog.Info("s3 watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close is a no-op for S3 (HTTP client managed by the AWS SDK).
func (w *S3Watcher) Close() error { return nil }

// ─── internal ─────────────────────────────────────────────────────────────────

// isManaged returns true if the S3 object at key is tagged with the VarTrack
// managed-by tag.
func (w *S3Watcher) isManaged(ctx context.Context, key string) (bool, error) {
	out, err := w.client.GetObjectTagging(ctx, &s3.GetObjectTaggingInput{
		Bucket: aws.String(w.bucket),
		Key:    aws.String(key),
	})
	if err != nil {
		return false, err
	}

	for _, tag := range out.TagSet {
		k := aws.ToString(tag.Key)
		v := aws.ToString(tag.Value)

		// Tags are URL-encoded — decode before comparing.
		dk, err1 := url.QueryUnescape(k)
		dv, err2 := url.QueryUnescape(v)
		if err1 != nil || err2 != nil {
			continue
		}
		if dk == "app.kubernetes.io/managed-by" && dv == ManagedByValue {
			return true, nil
		}
	}
	return false, nil
}
