// Calling the chain from a Java backend / TestNG test — plain REST, Java 11+, no extra deps.
// Start the server with:  python -m apichain.server --transport http --port 8765
//
// Option B (true MCP from Java): Spring AI MCP client (spring-ai-starter-mcp-client) pointed at
// http://host:8765/mcp — useful only if a Java-side LLM agent should pick tools itself.
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

public class ApiChainClient {
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    private final String baseUrl;

    public ApiChainClient(String baseUrl) { this.baseUrl = baseUrl; }

    /** inputsJson example: {"productCode":"UNL-5G","firstName":"John"} */
    public String runWorkflow(String name, String inputsJson) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(baseUrl + "/api/workflows/" + name + "/run"))
                .timeout(Duration.ofMinutes(2))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString("{\"inputs\":" + inputsJson + "}"))
                .build();
        HttpResponse<String> res = http.send(req, HttpResponse.BodyHandlers.ofString());
        if (res.statusCode() != 200) throw new IllegalStateException("Workflow failed: " + res.body());
        return res.body(); // parse with Jackson: outputs.orderId, outputs.accountId ...
    }

    public static void main(String[] args) throws Exception {
        String out = new ApiChainClient("http://127.0.0.1:8765")
                .runWorkflow("order_flow", "{\"productCode\":\"UNL-5G\"}");
        System.out.println(out);
    }
}
