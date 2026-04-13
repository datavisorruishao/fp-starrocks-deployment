package com.datavisor.smt;

import org.apache.kafka.common.config.ConfigDef;
import org.apache.kafka.connect.connector.ConnectRecord;
import org.apache.kafka.connect.data.Schema;
import org.apache.kafka.connect.data.SchemaBuilder;
import org.apache.kafka.connect.data.Struct;
import org.apache.kafka.connect.transforms.Transformation;

import java.sql.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Kafka Connect SMT that resolves integer-keyed featureMap fields to named, typed columns
 * by looking up Feature.id → Feature.name + Feature.return_type in MySQL.
 */
public class FeatureResolverTransform<R extends ConnectRecord<R>> implements Transformation<R> {

    private static final Logger log = LoggerFactory.getLogger(FeatureResolverTransform.class);

    private static final String JDBC_URL_CONFIG = "metadata.jdbc.url";
    private static final String JDBC_USER_CONFIG = "metadata.jdbc.user";
    private static final String JDBC_PASSWORD_CONFIG = "metadata.jdbc.password";
    private static final String REFRESH_INTERVAL_CONFIG = "metadata.refresh.interval.ms";
    private static final String FEATURE_MAP_FIELD_CONFIG = "feature.map.field";

    private String jdbcUrl;
    private String jdbcUser;
    private String jdbcPassword;
    private String featureMapField;

    // id (as string) → [name, return_type]
    private final ConcurrentHashMap<String, String[]> featureMetadata = new ConcurrentHashMap<>();

    // id (as string) → [name, type]
    private final ConcurrentHashMap<String, String[]> eventAttrMetadata = new ConcurrentHashMap<>();

    private Thread refreshThread;
    private final AtomicBoolean running = new AtomicBoolean(true);

    private volatile Schema cachedSchema;
    private volatile int metadataVersion = 0;
    private int lastSchemaVersion = -1;

    @Override
    public ConfigDef config() {
        return new ConfigDef()
                .define(JDBC_URL_CONFIG, ConfigDef.Type.STRING,
                        ConfigDef.Importance.HIGH, "JDBC URL for metadata database")
                .define(JDBC_USER_CONFIG, ConfigDef.Type.STRING, "root",
                        ConfigDef.Importance.HIGH, "JDBC username")
                .define(JDBC_PASSWORD_CONFIG, ConfigDef.Type.STRING, "",
                        ConfigDef.Importance.HIGH, "JDBC password")
                .define(REFRESH_INTERVAL_CONFIG, ConfigDef.Type.LONG, 60000L,
                        ConfigDef.Importance.LOW, "Metadata refresh interval in ms")
                .define(FEATURE_MAP_FIELD_CONFIG, ConfigDef.Type.STRING, "featureMap",
                        ConfigDef.Importance.MEDIUM, "Field name for the integer-keyed feature map");
    }

    @Override
    public void configure(Map<String, ?> configs) {
        jdbcUrl = configs.get(JDBC_URL_CONFIG).toString();
        jdbcUser = configs.containsKey(JDBC_USER_CONFIG) ? configs.get(JDBC_USER_CONFIG).toString() : "root";
        jdbcPassword = configs.containsKey(JDBC_PASSWORD_CONFIG) ? configs.get(JDBC_PASSWORD_CONFIG).toString() : "";
        featureMapField = configs.containsKey(FEATURE_MAP_FIELD_CONFIG)
                ? configs.get(FEATURE_MAP_FIELD_CONFIG).toString() : "featureMap";
        long refreshInterval = configs.containsKey(REFRESH_INTERVAL_CONFIG)
                ? Long.parseLong(configs.get(REFRESH_INTERVAL_CONFIG).toString()) : 60000L;

        try {
            Class.forName("com.mysql.cj.jdbc.Driver");
        } catch (ClassNotFoundException e) {
            throw new RuntimeException("MySQL JDBC driver not found on classpath", e);
        }

        log.info("Loading feature metadata from MySQL: {}", jdbcUrl);
        loadFeatureMetadata();
        log.info("Loaded {} feature mappings, {} event attr mappings", featureMetadata.size(), eventAttrMetadata.size());

        refreshThread = new Thread(() -> {
            while (running.get()) {
                try {
                    Thread.sleep(refreshInterval);
                    loadFeatureMetadata();
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                } catch (Exception e) {
                    log.warn("Error refreshing feature metadata from MySQL", e);
                }
            }
        }, "smt-feature-refresh");
        refreshThread.setDaemon(true);
        refreshThread.start();
    }

    private void loadFeatureMetadata() {
        String sql = "SELECT id, name, return_type FROM feature WHERE status = 'PUBLISHED'";
        try (Connection conn = DriverManager.getConnection(jdbcUrl, jdbcUser, jdbcPassword);
             PreparedStatement stmt = conn.prepareStatement(sql);
             ResultSet rs = stmt.executeQuery()) {

            while (rs.next()) {
                String id = String.valueOf(rs.getInt("id"));
                String name = rs.getString("name");
                String returnType = rs.getString("return_type");
                featureMetadata.put(id, new String[]{name, returnType});
            }
        } catch (SQLException e) {
            log.error("Failed to load feature metadata from MySQL", e);
        }

        String sql2 = "SELECT id, name, type FROM event_attribute_info";
        try (Connection conn = DriverManager.getConnection(jdbcUrl, jdbcUser, jdbcPassword);
             PreparedStatement stmt = conn.prepareStatement(sql2);
             ResultSet rs = stmt.executeQuery()) {

            while (rs.next()) {
                String id = String.valueOf(rs.getInt("id"));
                String name = rs.getString("name");
                String type = rs.getString("type");
                eventAttrMetadata.put(id, new String[]{name, type});
            }
            metadataVersion++;
        } catch (SQLException e) {
            log.error("Failed to load event_attribute_info from MySQL", e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public R apply(R record) {
        if (record.value() == null) return null;

        Map<String, Object> value;
        if (record.value() instanceof Map) {
            value = (Map<String, Object>) record.value();
        } else {
            log.warn("Unexpected value type: {}", record.value().getClass());
            return record;
        }

        if (lastSchemaVersion != metadataVersion) {
            cachedSchema = buildSchema();
            lastSchemaVersion = metadataVersion;
        }

        Schema schema = cachedSchema;
        Struct struct = new Struct(schema);

        // Copy fixed fields — all CH event_result fixed columns available in Kafka message
        // camelCase names avoid collision with feature/event-attr columns of the same name
        setIfPresent(struct, schema, "eventId",           toString(value.get("eventId")));
        setIfPresent(struct, schema, "eventType",          toString(value.get("eventType")));
        setIfPresent(struct, schema, "userId",             toString(value.get("userId")));
        setIfPresent(struct, schema, "eventTime",          toDate(value.get("time")));
        setIfPresent(struct, schema, "processingTime",     toDate(value.get("processTime")));
        setIfPresent(struct, schema, "rules",              toIntList(value.get("rules")));
        setIfPresent(struct, schema, "actions",            toStringList(value.get("actions")));
        setIfPresent(struct, schema, "trialRules",         toIntList(value.get("trialRules")));
        setIfPresent(struct, schema, "trialActions",       toStringList(value.get("trialActions")));
        setIfPresent(struct, schema, "reEvaluateEntity",   toString(value.get("reEvaluateEntity")));
        setIfPresent(struct, schema, "originId",           toInt(value.get("originId")));
        setIfPresent(struct, schema, "originCategory",     toString(value.get("originCategory")));
        setIfPresent(struct, schema, "fromUpdateAPI",      toBoolean(value.get("fromUpdateAPI")));

        // Resolve featureMap: {8: 100.50, 7: "US"} → {amount: 100.50 (double), country: "US" (string)}
        Object featureMapObj = value.get(featureMapField);
        if (featureMapObj instanceof Map) {
            Map<String, Object> featureMap = (Map<String, Object>) featureMapObj;
            for (Map.Entry<String, Object> entry : featureMap.entrySet()) {
                String[] meta = featureMetadata.get(entry.getKey());
                if (meta != null) {
                    setTypedField(struct, schema, meta[0], entry.getValue(), meta[1]);
                } else {
                    setIfPresent(struct, schema, "feature_" + entry.getKey(), toString(entry.getValue()));
                }
            }
        }

        // Resolve eventFields: {4: "u001", 18: "txn_001"} → named columns via event_attribute_info
        Object eventFieldsObj = value.get("eventFields");
        if (eventFieldsObj instanceof Map) {
            Map<String, Object> eventFields = (Map<String, Object>) eventFieldsObj;
            for (Map.Entry<String, Object> entry : eventFields.entrySet()) {
                String[] meta = eventAttrMetadata.get(entry.getKey());
                if (meta != null && schema.field(meta[0]) != null) {
                    setTypedField(struct, schema, meta[0], entry.getValue(), meta[1]);
                }
            }
        }

        return record.newRecord(
                record.topic(), record.kafkaPartition(),
                record.keySchema(), record.key(),
                schema, struct,
                record.timestamp()
        );
    }

    private Schema buildSchema() {
        SchemaBuilder builder = SchemaBuilder.struct().name("fp_event_result");

        // Fixed columns — camelCase to avoid collision with feature/event-attr columns
        builder.field("eventId",          Schema.OPTIONAL_STRING_SCHEMA);
        builder.field("eventType",         Schema.OPTIONAL_STRING_SCHEMA);
        builder.field("userId",            Schema.OPTIONAL_STRING_SCHEMA);
        builder.field("eventTime",         org.apache.kafka.connect.data.Timestamp.builder().optional().build());
        builder.field("processingTime",    org.apache.kafka.connect.data.Timestamp.builder().optional().build());
        builder.field("rules",             SchemaBuilder.array(Schema.INT32_SCHEMA).optional().build());
        builder.field("actions",           SchemaBuilder.array(Schema.STRING_SCHEMA).optional().build());
        builder.field("trialRules",        SchemaBuilder.array(Schema.INT32_SCHEMA).optional().build());
        builder.field("trialActions",      SchemaBuilder.array(Schema.STRING_SCHEMA).optional().build());
        builder.field("reEvaluateEntity",  Schema.OPTIONAL_STRING_SCHEMA);
        builder.field("originId",          Schema.OPTIONAL_INT32_SCHEMA);
        builder.field("originCategory",    Schema.OPTIONAL_STRING_SCHEMA);
        builder.field("fromUpdateAPI",     Schema.OPTIONAL_BOOLEAN_SCHEMA);

        // Feature/event-attribute columns with proper types from return_type
        Set<String> added = new HashSet<>(Arrays.asList(
                "eventId", "eventType", "userId", "eventTime", "processingTime",
                "rules", "actions", "trialRules", "trialActions", "reEvaluateEntity",
                "originId", "originCategory", "fromUpdateAPI"));

        for (String[] meta : featureMetadata.values()) {
            String name = meta[0];
            String returnType = meta[1];
            if (!added.contains(name)) {
                builder.field(name, toConnectSchema(returnType));
                added.add(name);
            }
        }

        // Event attribute columns (skip if already added by featureMetadata)
        for (String[] meta : eventAttrMetadata.values()) {
            String name = meta[0];
            String type = meta[1];
            if (!added.contains(name)) {
                builder.field(name, toConnectSchema(type));
                added.add(name);
            }
        }

        return builder.build();
    }

    /**
     * Maps FP return_type to Kafka Connect schema.
     * Complex types (List, Set, Map, JSONArray, JSONObject) are stored as strings.
     */
    private Schema toConnectSchema(String returnType) {
        if (returnType == null) return Schema.OPTIONAL_STRING_SCHEMA;
        switch (returnType) {
            case "Double":
            case "double":  return Schema.OPTIONAL_FLOAT64_SCHEMA;
            case "Float":
            case "float":   return Schema.OPTIONAL_FLOAT32_SCHEMA;
            case "Integer":
            case "integer":
            case "int":     return Schema.OPTIONAL_INT32_SCHEMA;
            case "Long":
            case "long":    return Schema.OPTIONAL_INT64_SCHEMA;
            case "Boolean":
            case "boolean": return Schema.OPTIONAL_BOOLEAN_SCHEMA;
            default:        return Schema.OPTIONAL_STRING_SCHEMA;  // String, List<*>, Set<*>, JSONArray, etc.
        }
    }

    private void setTypedField(Struct struct, Schema schema, String fieldName, Object value, String returnType) {
        if (schema.field(fieldName) == null || value == null) return;
        try {
            switch (returnType) {
                case "Double":
                case "double":  struct.put(fieldName, toDouble(value)); break;
                case "Float":
                case "float":   struct.put(fieldName, toFloat(value)); break;
                case "Integer":
                case "integer":
                case "int":     struct.put(fieldName, toInt(value)); break;
                case "Long":
                case "long":    struct.put(fieldName, toLong(value)); break;
                case "Boolean":
                case "boolean": struct.put(fieldName, toBoolean(value)); break;
                default:        struct.put(fieldName, toString(value)); break;
            }
        } catch (Exception e) {
            log.warn("Failed to convert field {}={} as {}", fieldName, value, returnType, e);
        }
    }

    private void setIfPresent(Struct struct, Schema schema, String fieldName, Object value) {
        if (schema.field(fieldName) != null && value != null) {
            struct.put(fieldName, value);
        }
    }

    private Long toLong(Object v) {
        if (v == null) return null;
        if (v instanceof Number) return ((Number) v).longValue();
        return Long.parseLong(v.toString());
    }

    /**
     * Convert a seconds-or-milliseconds epoch value to java.util.Date (milliseconds).
     * Values > 1_000_000_000_000 are treated as milliseconds, otherwise seconds.
     * Kafka Connect Timestamp schema accepts java.util.Date internally as milliseconds.
     */
    private java.util.Date toDate(Object v) {
        if (v == null) return null;
        long val = toLong(v);
        long ms = val > 1_000_000_000_000L ? val : val * 1000L;
        return new java.util.Date(ms);
    }

    private Double toDouble(Object v) {
        if (v == null) return null;
        if (v instanceof Number) return ((Number) v).doubleValue();
        String s = v.toString();
        if (s.equals("Infinity") || s.equals("+Infinity") || s.equals("-Infinity") || s.equals("NaN"))
            return null;  // Parquet rejects these
        return Double.parseDouble(s);
    }

    private Float toFloat(Object v) {
        if (v == null) return null;
        if (v instanceof Number) return ((Number) v).floatValue();
        return Float.parseFloat(v.toString());
    }

    private Integer toInt(Object v) {
        if (v == null) return null;
        if (v instanceof Number) return ((Number) v).intValue();
        return Integer.parseInt(v.toString());
    }

    private Boolean toBoolean(Object v) {
        if (v == null) return null;
        if (v instanceof Boolean) return (Boolean) v;
        return Boolean.parseBoolean(v.toString());
    }

    private String toString(Object v) {
        return v == null ? null : v.toString();
    }

    private List<Integer> toIntList(Object v) {
        if (!(v instanceof List)) return null;
        List<?> list = (List<?>) v;
        List<Integer> result = new ArrayList<>(list.size());
        for (Object item : list) {
            if (item instanceof Number) result.add(((Number) item).intValue());
            else if (item != null) result.add(Integer.parseInt(item.toString()));
        }
        return result;
    }

    private List<String> toStringList(Object v) {
        if (!(v instanceof List)) return null;
        List<?> list = (List<?>) v;
        List<String> result = new ArrayList<>(list.size());
        for (Object item : list) result.add(item == null ? null : item.toString());
        return result;
    }

    @Override
    public void close() {
        running.set(false);
        if (refreshThread != null) refreshThread.interrupt();
    }
}
